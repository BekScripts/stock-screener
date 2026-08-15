"""The financial quality component: 25 points from four metrics."""

from __future__ import annotations

import pytest

from domain import ComponentStatus, score_quality


def _sub(component, name):
    return next(sub for sub in component.subscores if sub.name == name)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("margin", "expected"),
    [
        (0.05, 0.0),
        (0.10, 1.0),
        (0.20, 2.0),
        (0.30, 3.0),
        (0.40, 4.0),
        (0.50, 5.0),
        (0.65, 6.0),
        (0.90, 6.0),
    ],
)
def test_gross_margin_level_scores_its_bands(margin: float, expected: float, make) -> None:
    component = score_quality(make.metrics(gross_margin=margin, gross_margin_change=0.0))

    assert _sub(component, "gross_margin").points == pytest.approx(expected)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("change", "expected"),
    [(-0.05, 4.0), (-0.03, 4.0), (-0.015, 4.5), (0.0, 5.0), (0.015, 5.5), (0.03, 6.0), (0.10, 6.0)],
)
def test_gross_margin_trend_adjusts_the_level(change: float, expected: float, make) -> None:
    component = score_quality(make.metrics(gross_margin=0.55, gross_margin_change=change))

    assert _sub(component, "gross_margin").points == pytest.approx(expected)


@pytest.mark.unit
def test_a_strong_improving_gross_margin_earns_the_full_seven(make) -> None:
    component = score_quality(make.metrics(gross_margin=0.70, gross_margin_change=0.05))

    assert _sub(component, "gross_margin").points == 7.0


@pytest.mark.unit
def test_gross_margin_scores_the_level_alone_when_no_trend_is_available(make) -> None:
    component = score_quality(make.metrics(gross_margin=0.55, gross_margin_change=None))

    subscore = _sub(component, "gross_margin")
    assert subscore.points == 5.0
    assert subscore.note is not None
    assert "level only" in subscore.note


@pytest.mark.unit
@pytest.mark.parametrize(
    ("fcf_margin", "expected"),
    [
        (-0.50, 0.0),
        (-0.30, 0.0),
        (-0.20, 1.0),
        (-0.10, 2.0),
        (0.0, 3.0),
        (0.05, 4.0),
        (0.10, 5.0),
        (0.15, 6.0),
        (0.25, 7.0),
        (0.60, 7.0),
        (-0.15, 1.5),
        (0.20, 6.5),
    ],
)
def test_fcf_margin_scores_its_reference_points(fcf_margin: float, expected: float, make) -> None:
    component = score_quality(make.metrics(fcf_margin=fcf_margin, fcf_margin_change=0.0))

    assert _sub(component, "fcf_margin").points == pytest.approx(expected)


@pytest.mark.unit
def test_a_burning_company_whose_burn_is_narrowing_earns_a_point(make) -> None:
    stable = score_quality(make.metrics(fcf_margin=-0.10, fcf_margin_change=0.0))
    improving = score_quality(make.metrics(fcf_margin=-0.10, fcf_margin_change=0.08))

    assert _sub(stable, "fcf_margin").points == 2.0
    assert _sub(improving, "fcf_margin").points == 3.0


@pytest.mark.unit
def test_a_profitable_company_gets_no_improvement_bonus(make) -> None:
    component = score_quality(make.metrics(fcf_margin=0.10, fcf_margin_change=0.20))

    assert _sub(component, "fcf_margin").points == 5.0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("net_cash_ratio", "expected"),
    [
        (-1.0, 0.0),
        (-0.75, 0.0),
        (-0.50, 1.0),
        (-0.25, 2.0),
        (-0.10, 3.0),
        (0.0, 4.0),
        (0.10, 5.0),
        (0.20, 6.0),
        (0.50, 6.0),
    ],
)
def test_cash_against_debt_scores_relative_to_market_cap(
    net_cash_ratio: float, expected: float, make
) -> None:
    market_cap = 1_000_000_000.0
    component = score_quality(
        make.metrics(market_cap=market_cap, net_cash=net_cash_ratio * market_cap)
    )

    assert _sub(component, "cash_vs_debt").points == pytest.approx(expected)


@pytest.mark.unit
def test_the_same_net_debt_scores_differently_at_different_scales(make) -> None:
    # $500M of net debt is most of a small company and a rounding error for a
    # large one. Scoring the dollar figure would rank companies by size.
    small = score_quality(make.metrics(market_cap=1_000_000_000.0, net_cash=-500_000_000.0))
    large = score_quality(make.metrics(market_cap=50_000_000_000.0, net_cash=-500_000_000.0))

    assert _sub(small, "cash_vs_debt").points == 1.0
    assert _sub(large, "cash_vs_debt").points == pytest.approx(3.9, abs=0.05)


@pytest.mark.unit
def test_cash_against_debt_is_unavailable_when_debt_is_unknown(make) -> None:
    component = score_quality(make.metrics(debt=None, net_cash=None))

    assert _sub(component, "cash_vs_debt").points is None
    assert component.status is ComponentStatus.SCORED
    assert component.redistributed is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (-0.20, 0.0),
        (-0.10, 0.0),
        (-0.05, 1.0),
        (0.0, 2.0),
        (0.03, 3.0),
        (0.05, 4.0),
        (0.10, 5.0),
        (0.30, 5.0),
    ],
)
def test_operating_margin_improvement_scores_its_reference_points(
    change: float, expected: float, make
) -> None:
    component = score_quality(make.metrics(operating_margin_change=change))

    assert _sub(component, "operating_margin_trend").points == pytest.approx(expected)


@pytest.mark.unit
def test_an_unprofitable_company_still_earns_points_for_improving(make) -> None:
    component = score_quality(make.metrics(operating_margin=-0.15, operating_margin_change=0.06))

    assert _sub(component, "operating_margin_trend").points == pytest.approx(4.2)


@pytest.mark.unit
def test_quality_cannot_be_scored_when_only_a_quarter_of_it_is_known(make) -> None:
    component = score_quality(
        make.metrics(gross_margin=None, fcf_margin=None, operating_margin_change=None)
    )

    assert component.status is ComponentStatus.INSUFFICIENT_DATA


@pytest.mark.unit
def test_a_flawless_balance_sheet_scores_the_full_component(make) -> None:
    metrics = make.metrics(
        gross_margin=0.80,
        gross_margin_change=0.05,
        fcf_margin=0.30,
        market_cap=1_000_000_000.0,
        net_cash=400_000_000.0,
        operating_margin_change=0.15,
    )

    assert score_quality(metrics).score == 25.0
