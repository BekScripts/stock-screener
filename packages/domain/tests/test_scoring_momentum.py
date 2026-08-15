"""The market confirmation component: 15 points, relative to the benchmark."""

from __future__ import annotations

import pytest

from domain import BenchmarkReturns, ComponentStatus, relative_strength, score_momentum


def _sub(component, name):
    return next(sub for sub in component.subscores if sub.name == name)


@pytest.mark.unit
def test_relative_strength_is_the_difference_between_two_returns() -> None:
    assert relative_strength(0.30, 0.10) == pytest.approx(0.20)


@pytest.mark.unit
def test_relative_strength_is_unknown_without_a_benchmark_return() -> None:
    assert relative_strength(0.30, None) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("strength", "expected"),
    [
        (-0.60, 0.0),
        (-0.30, 0.0),
        (-0.15, 1.0),
        (0.0, 3.0),
        (0.15, 5.0),
        (0.30, 6.0),
        (1.0, 6.0),
        (0.075, 4.0),
        (0.20, 5.333),
    ],
)
def test_six_month_relative_strength_scores_its_reference_points(
    strength: float, expected: float, make
) -> None:
    benchmark = make.benchmark(return_6m=0.10)
    component = score_momentum(make.metrics(return_6m=0.10 + strength), benchmark)

    assert _sub(component, "relative_strength_6m").points == pytest.approx(expected, abs=1e-3)


@pytest.mark.unit
def test_twelve_month_relative_strength_uses_the_same_curve(make) -> None:
    benchmark = make.benchmark(return_12m=0.10)
    component = score_momentum(make.metrics(return_12m=0.25), benchmark)

    assert _sub(component, "relative_strength_12m").points == pytest.approx(5.0)


@pytest.mark.unit
def test_a_stock_matching_the_market_scores_the_middle_of_the_range(make) -> None:
    benchmark = BenchmarkReturns(symbol="SPY", return_6m=0.12, return_12m=0.18)
    component = score_momentum(make.metrics(return_6m=0.12, return_12m=0.18), benchmark)

    assert _sub(component, "relative_strength_6m").points == 3.0
    assert _sub(component, "relative_strength_12m").points == 3.0


@pytest.mark.unit
def test_a_stock_lagging_the_market_scores_poorly_without_being_excluded(make) -> None:
    benchmark = make.benchmark(return_6m=0.20, return_12m=0.30)
    component = score_momentum(make.metrics(return_6m=-0.10, return_12m=-0.05), benchmark)

    assert component.status is ComponentStatus.SCORED
    assert _sub(component, "relative_strength_6m").points == 0.0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("distance", "expected"),
    [
        (-0.80, 0.0),
        (-0.50, 0.0),
        (-0.35, 1.0),
        (-0.20, 2.0),
        (-0.10, 2.5),
        (-0.05, 3.0),
        (0.0, 3.0),
    ],
)
def test_the_52_week_position_scores_its_reference_points(
    distance: float, expected: float, make
) -> None:
    component = score_momentum(make.metrics(distance_from_52w_high=distance), make.benchmark())

    assert _sub(component, "position_52w").points == pytest.approx(expected)


@pytest.mark.unit
def test_momentum_cannot_be_scored_without_a_benchmark(make) -> None:
    component = score_momentum(make.metrics(), benchmark=None)

    assert component.status is ComponentStatus.INSUFFICIENT_DATA
    assert _sub(component, "relative_strength_6m").points is None


@pytest.mark.unit
def test_a_stock_with_only_six_months_of_history_still_scores(make) -> None:
    component = score_momentum(make.metrics(return_12m=None), make.benchmark())

    assert component.status is ComponentStatus.SCORED
    assert component.redistributed is True


@pytest.mark.unit
def test_market_confirmation_records_which_benchmark_it_used(make) -> None:
    component = score_momentum(make.metrics(), make.benchmark())

    assert _sub(component, "relative_strength_6m").note == "against SPY"
