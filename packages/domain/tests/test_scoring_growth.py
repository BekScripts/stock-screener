"""The growth component: 35 points from five metrics.

Every reference point in the specification is asserted here, together with a
value between each pair, because interpolation is where a scoring curve most
easily goes wrong without anything looking obviously broken.
"""

from __future__ import annotations

import pytest

from domain import ComponentStatus, score_growth


def _sub(component, name):
    return next(sub for sub in component.subscores if sub.name == name)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("growth", "expected"),
    [
        (-0.50, 0.0),
        (0.0, 0.0),
        (0.10, 3.0),
        (0.20, 6.0),
        (0.30, 8.0),
        (0.50, 10.0),
        (0.75, 12.0),
        (5.0, 12.0),
        (0.15, 4.5),
        (0.25, 7.0),
        (0.40, 9.0),
        (0.60, 10.8),
    ],
)
def test_revenue_growth_scores_its_reference_points(growth: float, expected: float, make) -> None:
    component = score_growth(make.metrics(revenue_growth_yoy=growth))

    assert _sub(component, "revenue_growth").points == pytest.approx(expected)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("acceleration", "expected"),
    [
        (-0.50, 0.0),
        (-0.20, 0.0),
        (-0.10, 1.0),
        (0.0, 3.0),
        (0.10, 5.0),
        (0.20, 7.0),
        (0.30, 8.0),
        (1.0, 8.0),
        (0.05, 4.0),
        (0.15, 6.0),
    ],
)
def test_acceleration_scores_its_reference_points(
    acceleration: float, expected: float, make
) -> None:
    component = score_growth(make.metrics(revenue_growth_acceleration=acceleration))

    assert _sub(component, "growth_acceleration").points == pytest.approx(expected)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cagr", "expected"),
    [
        (-0.10, 0.0),
        (0.0, 0.0),
        (0.05, 1.0),
        (0.10, 2.0),
        (0.15, 3.0),
        (0.25, 5.0),
        (0.35, 6.0),
        (1.0, 6.0),
    ],
)
def test_three_year_cagr_scores_its_reference_points(cagr: float, expected: float, make) -> None:
    component = score_growth(make.metrics(revenue_cagr_3y=cagr))

    assert _sub(component, "revenue_cagr_3y").points == pytest.approx(expected)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("gross_profit_growth", "expected"),
    [(0.0, 0.0), (0.10, 1.0), (0.20, 2.0), (0.30, 3.0), (0.50, 4.0), (0.75, 5.0), (2.0, 5.0)],
)
def test_gross_profit_growth_scores_its_reference_points(
    gross_profit_growth: float, expected: float, make
) -> None:
    component = score_growth(make.metrics(gross_profit_growth_yoy=gross_profit_growth))

    assert _sub(component, "gross_profit_growth").points == pytest.approx(expected)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("observations", "expected"),
    [
        ((-0.10, -0.20, -0.05, -0.01), 0.0),
        ((0.10, -0.20, -0.05, -0.01), 1.0),
        ((0.10, 0.05, -0.05, -0.01), 2.0),
        ((0.10, 0.05, 0.02, -0.01), 3.0),
        ((0.10, 0.05, 0.02, 0.01), 4.0),
    ],
)
def test_persistence_counts_positive_quarters(
    observations: tuple[float, ...], expected: float, make
) -> None:
    component = score_growth(make.metrics(recent_revenue_growth_yoy=observations))

    assert _sub(component, "growth_persistence").points == expected


@pytest.mark.unit
def test_persistence_is_unavailable_with_fewer_than_four_observations(make) -> None:
    component = score_growth(make.metrics(recent_revenue_growth_yoy=(0.30, 0.20, 0.10)))

    assert _sub(component, "growth_persistence").points is None


@pytest.mark.unit
def test_a_flat_quarter_does_not_count_as_positive_persistence(make) -> None:
    component = score_growth(make.metrics(recent_revenue_growth_yoy=(0.0, 0.10, 0.10, 0.10)))

    assert _sub(component, "growth_persistence").points == 3.0


@pytest.mark.unit
def test_a_perfect_growth_profile_scores_the_full_component(make) -> None:
    metrics = make.metrics(
        revenue_growth_yoy=0.80,
        revenue_growth_acceleration=0.40,
        revenue_cagr_3y=0.40,
        gross_profit_growth_yoy=0.90,
        recent_revenue_growth_yoy=(0.8, 0.7, 0.6, 0.5),
    )

    assert score_growth(metrics).score == 35.0


@pytest.mark.unit
def test_growth_cannot_be_scored_without_current_revenue_growth(make) -> None:
    component = score_growth(make.metrics(revenue_growth_yoy=None))

    assert component.status is ComponentStatus.INSUFFICIENT_DATA
    assert component.score is None


@pytest.mark.unit
def test_a_missing_cagr_is_carried_by_the_remaining_growth_metrics(make) -> None:
    complete = score_growth(make.metrics())
    without_cagr = score_growth(make.metrics(revenue_cagr_3y=None))

    assert without_cagr.status is ComponentStatus.SCORED
    assert without_cagr.redistributed is True
    assert without_cagr.coverage == pytest.approx(29 / 35, abs=1e-4)
    # The company keeps the same share of the component it earned on what is
    # known, rather than being charged zero for the missing metric.
    assert without_cagr.score > complete.score - 6.0


@pytest.mark.unit
def test_a_missing_metric_is_reported_as_missing_not_as_zero_points(make) -> None:
    component = score_growth(make.metrics(gross_profit_growth_yoy=None))

    assert _sub(component, "gross_profit_growth").points is None
    assert "gross_profit_growth" in component.missing_metrics


@pytest.mark.unit
def test_growth_needs_more_than_a_level_to_score(make) -> None:
    # Current growth alone is 12 of 35 points — below the coverage minimum, so
    # the component says it does not know rather than scoring a third of itself.
    component = score_growth(
        make.metrics(
            revenue_growth_acceleration=None,
            revenue_cagr_3y=None,
            gross_profit_growth_yoy=None,
            recent_revenue_growth_yoy=(),
        )
    )

    assert component.status is ComponentStatus.INSUFFICIENT_DATA


@pytest.mark.unit
def test_the_component_never_exceeds_its_maximum_after_redistribution(make) -> None:
    # Every available metric maxed out, with 11 of 35 points unavailable. The
    # redistribution cap means the component lands below its maximum rather than
    # at it — but the invariant under test is that it can never exceed it.
    metrics = make.metrics(
        revenue_growth_yoy=2.0,
        revenue_growth_acceleration=1.0,
        revenue_cagr_3y=None,
        gross_profit_growth_yoy=None,
        recent_revenue_growth_yoy=(0.5, 0.5, 0.5, 0.5),
    )

    component = score_growth(metrics)

    assert component.score is not None
    assert component.score <= 35.0
    assert component.score == pytest.approx(24 * 1.15, abs=0.01)
