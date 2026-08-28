"""The three V1.1 guards, individually and stacked.

Each guard exists because a real market measurement showed V1 rewarding
something it did not mean to. What the tests protect is the trigger condition —
above all the cases that must *not* fire, since a guard that fires on unknown
data would reintroduce the "missing is not zero" failure one layer up.
"""

from __future__ import annotations

import pytest

from domain import (
    COMPOUNDER_V1_2,
    CURRENT_SCORE_VERSION,
    GROWTH_MAX,
    MAX_REDISTRIBUTION_MULTIPLIER,
    MOMENTUM_MAX,
    QUALITY_MAX,
    VALUATION_MAX,
    ComponentStatus,
    is_cyclical_rebound,
    score_company,
    score_growth,
    score_momentum,
    score_quality,
    score_valuation,
)


def _sub(component, name):
    return next(sub for sub in component.subscores if sub.name == name)


# -- A1: redistribution cap --------------------------------------------------


@pytest.mark.unit
def test_a_component_missing_nothing_is_not_scaled(make) -> None:
    component = score_growth(make.metrics())

    assert component.redistributed is False
    assert component.coverage == 1.0


@pytest.mark.unit
def test_redistribution_below_the_cap_is_applied_in_full(make) -> None:
    # Missing 4 of 35 points is a multiplier of 35/31 = 1.129, inside the cap.
    component = score_growth(make.metrics(recent_revenue_growth_yoy=()))
    earned = sum(sub.points for sub in component.subscores if sub.points is not None)

    assert component.score == pytest.approx(earned * (35.0 / 31.0), abs=0.01)


@pytest.mark.unit
def test_redistribution_above_the_cap_is_limited(make) -> None:
    # Missing gross margin is 7 of 25 quality points: 25/18 = 1.389, well over.
    component = score_quality(make.metrics(gross_margin=None, gross_margin_change=None))
    earned = sum(sub.points for sub in component.subscores if sub.points is not None)

    assert component.score == pytest.approx(earned * MAX_REDISTRIBUTION_MULTIPLIER, abs=0.01)
    assert component.score < earned * (25.0 / 18.0)


@pytest.mark.unit
def test_the_cap_binds_exactly_at_its_boundary(make) -> None:
    # 6 of 35 missing is 35/29 = 1.207, above the cap; the scale factor used
    # must be the cap itself rather than the uncapped ratio.
    component = score_growth(make.metrics(revenue_cagr_3y=None))
    earned = sum(sub.points for sub in component.subscores if sub.points is not None)

    assert component.score == pytest.approx(earned * MAX_REDISTRIBUTION_MULTIPLIER, abs=0.01)


@pytest.mark.unit
def test_component_maximums_are_unchanged(make) -> None:
    metrics = make.metrics()

    growth = score_growth(metrics)
    quality = score_quality(metrics)
    valuation, _ = score_valuation(metrics)
    momentum = score_momentum(metrics, make.benchmark())

    assert (growth.max_points, quality.max_points) == (GROWTH_MAX, QUALITY_MAX)
    assert (valuation.max_points, momentum.max_points) == (VALUATION_MAX, MOMENTUM_MAX)
    assert GROWTH_MAX + QUALITY_MAX + VALUATION_MAX + MOMENTUM_MAX == 100.0


@pytest.mark.unit
def test_the_cap_never_lifts_a_component_over_its_maximum(make) -> None:
    metrics = make.metrics(
        revenue_growth_yoy=2.0,
        revenue_growth_acceleration=1.0,
        revenue_cagr_3y=None,
        recent_revenue_growth_yoy=(0.5, 0.5, 0.5, 0.5),
    )

    assert score_growth(metrics).score <= GROWTH_MAX


# -- B3: cyclical rebound guard ---------------------------------------------


@pytest.mark.unit
def test_a_rebound_loses_its_acceleration_bonus(make) -> None:
    metrics = make.metrics(
        revenue_growth_yoy=0.60, revenue_cagr_3y=0.01, revenue_growth_acceleration=0.25
    )

    subscore = _sub(score_growth(metrics), "growth_acceleration")

    assert subscore.points == 3.0
    assert subscore.note is not None
    assert "rebound" in subscore.note


@pytest.mark.unit
def test_a_rebound_keeps_its_revenue_growth_points(make) -> None:
    durable = make.metrics(
        revenue_growth_yoy=0.60, revenue_cagr_3y=0.30, revenue_growth_acceleration=0.25
    )
    rebound = make.metrics(
        revenue_growth_yoy=0.60, revenue_cagr_3y=0.01, revenue_growth_acceleration=0.25
    )

    assert _sub(score_growth(rebound), "revenue_growth").points == pytest.approx(
        _sub(score_growth(durable), "revenue_growth").points
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("yoy", "cagr", "expected"),
    [
        (0.60, 0.01, True),
        (0.30, 0.05, True),
        (0.29, 0.01, False),
        (0.60, 0.06, False),
        (0.60, 0.30, False),
        (0.10, -0.10, False),
    ],
)
def test_the_rebound_trigger_is_exact(yoy: float, cagr: float, expected: bool, make) -> None:
    metrics = make.metrics(revenue_growth_yoy=yoy, revenue_cagr_3y=cagr)

    assert is_cyclical_rebound(metrics) is expected


@pytest.mark.unit
def test_an_unknown_cagr_does_not_trigger_the_rebound_guard(make) -> None:
    metrics = make.metrics(
        revenue_growth_yoy=0.60, revenue_cagr_3y=None, revenue_growth_acceleration=0.25
    )

    assert is_cyclical_rebound(metrics) is False
    # +25pp interpolates to 7.5 on the curve — well above the 3.0 cap the guard
    # would have imposed, which is the point: it did not fire.
    assert _sub(score_growth(metrics), "growth_acceleration").points == pytest.approx(7.5)


@pytest.mark.unit
def test_a_rebound_with_weak_acceleration_is_not_raised_to_the_cap(make) -> None:
    # The guard is a ceiling, never a floor.
    metrics = make.metrics(
        revenue_growth_yoy=0.60, revenue_cagr_3y=0.01, revenue_growth_acceleration=-0.15
    )

    assert _sub(score_growth(metrics), "growth_acceleration").points == pytest.approx(0.5)


# -- C2: lumpy growth guard --------------------------------------------------


@pytest.mark.unit
def test_lumpy_growth_is_scaled_to_the_combined_budget(make) -> None:
    metrics = make.metrics(
        revenue_growth_yoy=1.00,
        revenue_cagr_3y=0.40,
        revenue_growth_acceleration=0.40,
        recent_revenue_growth_yoy=(1.00, -0.10, -0.20, 0.05),
    )

    component = score_growth(metrics)
    level = _sub(component, "revenue_growth")
    acceleration = _sub(component, "growth_acceleration")

    assert level.points + acceleration.points == pytest.approx(12.0)
    # Scaled proportionally: 12 and 8 become 7.2 and 4.8.
    assert level.points == pytest.approx(7.2)
    assert acceleration.points == pytest.approx(4.8)
    assert "scaled" in (level.note or "")


@pytest.mark.unit
def test_persistence_of_three_does_not_trigger_the_lumpy_guard(make) -> None:
    metrics = make.metrics(
        revenue_growth_yoy=1.00,
        revenue_cagr_3y=0.40,
        revenue_growth_acceleration=0.40,
        recent_revenue_growth_yoy=(1.00, 0.10, 0.20, -0.05),
    )

    component = score_growth(metrics)

    assert _sub(component, "revenue_growth").points == 12.0
    assert _sub(component, "growth_acceleration").points == 8.0


@pytest.mark.unit
def test_unknown_persistence_does_not_trigger_the_lumpy_guard(make) -> None:
    metrics = make.metrics(
        revenue_growth_yoy=1.00,
        revenue_cagr_3y=0.40,
        revenue_growth_acceleration=0.40,
        recent_revenue_growth_yoy=(1.00, 0.10),
    )

    component = score_growth(metrics)

    assert _sub(component, "growth_persistence").points is None
    assert _sub(component, "revenue_growth").points == 12.0
    assert _sub(component, "growth_acceleration").points == 8.0


@pytest.mark.unit
def test_lumpy_growth_below_the_budget_is_untouched(make) -> None:
    # 6 + 3 = 9 points, under the 12-point budget, so the guard does not bind.
    metrics = make.metrics(
        revenue_growth_yoy=0.20,
        revenue_cagr_3y=0.10,
        revenue_growth_acceleration=0.0,
        recent_revenue_growth_yoy=(0.20, -0.10, -0.20, 0.05),
    )

    component = score_growth(metrics)

    assert _sub(component, "revenue_growth").points == 6.0
    assert _sub(component, "growth_acceleration").points == 3.0


# -- stacking ----------------------------------------------------------------


@pytest.mark.unit
def test_the_rebound_and_lumpy_guards_stack(make) -> None:
    # Rebound first caps acceleration at 3.0, then the pair (12 + 3 = 15) is
    # scaled to 12 — so the second guard operates on the first one's output.
    metrics = make.metrics(
        revenue_growth_yoy=1.00,
        revenue_cagr_3y=0.01,
        revenue_growth_acceleration=0.40,
        recent_revenue_growth_yoy=(1.00, -0.10, -0.20, 0.05),
    )

    component = score_growth(metrics)
    level = _sub(component, "revenue_growth")
    acceleration = _sub(component, "growth_acceleration")

    assert level.points + acceleration.points == pytest.approx(12.0)
    assert level.points == pytest.approx(9.6)
    assert acceleration.points == pytest.approx(2.4)


@pytest.mark.unit
def test_the_redistribution_cap_stacks_with_the_rebound_guard(make) -> None:
    metrics = make.metrics(
        revenue_growth_yoy=0.60,
        revenue_cagr_3y=0.01,
        revenue_growth_acceleration=0.25,
        gross_profit_growth_yoy=None,
    )

    component = score_growth(metrics)
    earned = sum(sub.points for sub in component.subscores if sub.points is not None)

    assert _sub(component, "growth_acceleration").points == 3.0
    assert component.score == pytest.approx(earned * MAX_REDISTRIBUTION_MULTIPLIER, abs=0.01)


@pytest.mark.unit
def test_the_redistribution_cap_stacks_with_the_lumpy_guard(make) -> None:
    metrics = make.metrics(
        revenue_growth_yoy=1.00,
        revenue_cagr_3y=0.40,
        revenue_growth_acceleration=0.40,
        recent_revenue_growth_yoy=(1.00, -0.10, -0.20, 0.05),
        gross_profit_growth_yoy=None,
    )

    component = score_growth(metrics)
    earned = sum(sub.points for sub in component.subscores if sub.points is not None)

    assert _sub(component, "revenue_growth").points + _sub(
        component, "growth_acceleration"
    ).points == pytest.approx(12.0)
    assert component.score == pytest.approx(earned * MAX_REDISTRIBUTION_MULTIPLIER, abs=0.01)


@pytest.mark.unit
def test_all_three_guards_stack_without_a_combined_cap(make) -> None:
    metrics = make.metrics(
        revenue_growth_yoy=1.00,
        revenue_cagr_3y=0.01,
        revenue_growth_acceleration=0.40,
        recent_revenue_growth_yoy=(1.00, -0.10, -0.20, 0.05),
        gross_profit_growth_yoy=None,
        gross_margin=None,
        gross_margin_change=None,
    )

    growth = score_growth(metrics)
    quality = score_quality(metrics)
    growth_earned = sum(sub.points for sub in growth.subscores if sub.points is not None)
    quality_earned = sum(sub.points for sub in quality.subscores if sub.points is not None)

    # 12 from the scaled level+acceleration pair, 0.2 for a 1% three-year CAGR,
    # and 2.0 for two positive quarters out of four.
    assert growth_earned == pytest.approx(14.2)
    assert growth.score == pytest.approx(growth_earned * MAX_REDISTRIBUTION_MULTIPLIER, abs=0.01)
    assert quality.score == pytest.approx(quality_earned * MAX_REDISTRIBUTION_MULTIPLIER, abs=0.01)
    assert growth.status is ComponentStatus.SCORED


@pytest.mark.unit
def test_a_score_is_stamped_with_the_current_version(make) -> None:
    # The guards in this module arrived with V1.1 and are unchanged since. What
    # a fresh score is *stamped* with is whatever version is current — V1.2 today
    # — because a snapshot has to record the rules it was produced under.
    score = score_company(make.profile(), make.metrics(), make.benchmark())

    assert score.score_version == CURRENT_SCORE_VERSION
    assert CURRENT_SCORE_VERSION == COMPOUNDER_V1_2


@pytest.mark.unit
def test_a_durable_grower_is_untouched_by_every_guard(make) -> None:
    metrics = make.metrics(
        revenue_growth_yoy=0.60,
        revenue_cagr_3y=0.40,
        revenue_growth_acceleration=0.25,
        recent_revenue_growth_yoy=(0.60, 0.50, 0.45, 0.40),
    )

    component = score_growth(metrics)

    assert _sub(component, "revenue_growth").points == pytest.approx(10.8)
    assert _sub(component, "growth_acceleration").points == pytest.approx(7.5)
    # Full coverage, so nothing is scaled and no guard leaves a note.
    assert component.coverage == 1.0
    assert component.redistributed is False
    assert all(sub.note is None or "quarters observed" in sub.note for sub in component.subscores)
    assert component.score == pytest.approx(31.3, abs=0.05)
