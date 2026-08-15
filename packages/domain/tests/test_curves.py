"""The interpolation and band primitives every scoring rule is built from."""

from __future__ import annotations

import pytest

from domain import CurveError, band, interpolate

CURVE = ((0.0, 0.0), (0.10, 3.0), (0.20, 6.0), (0.30, 8.0), (0.50, 10.0), (0.75, 12.0))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.0, 0.0), (0.10, 3.0), (0.20, 6.0), (0.30, 8.0), (0.50, 10.0), (0.75, 12.0)],
)
def test_returns_the_stated_points_at_every_reference_point(value: float, expected: float) -> None:
    assert interpolate(CURVE, value) == pytest.approx(expected)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.05, 1.5),  # halfway between 0 and 10%
        (0.15, 4.5),
        (0.25, 7.0),
        (0.40, 9.0),
        (0.60, 10.8),
    ],
)
def test_interpolates_linearly_between_reference_points(value: float, expected: float) -> None:
    assert interpolate(CURVE, value) == pytest.approx(expected)


@pytest.mark.unit
def test_clamps_below_the_first_reference_point() -> None:
    assert interpolate(CURVE, -5.0) == 0.0


@pytest.mark.unit
def test_clamps_above_the_last_reference_point() -> None:
    assert interpolate(CURVE, 50.0) == 12.0


@pytest.mark.unit
def test_handles_a_curve_whose_points_descend_in_value() -> None:
    penalties = ((0.02, 0.0), (0.10, -3.0), (0.35, -10.0))

    assert interpolate(penalties, 0.06) == pytest.approx(-1.5)


@pytest.mark.unit
def test_rejects_a_curve_with_one_point() -> None:
    with pytest.raises(CurveError, match="at least two reference points"):
        interpolate(((1.0, 1.0),), 1.0)


@pytest.mark.unit
def test_rejects_a_curve_whose_points_do_not_ascend() -> None:
    with pytest.raises(CurveError, match="must ascend"):
        interpolate(((0.20, 1.0), (0.10, 2.0)), 0.15)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.70, 6.0), (0.65, 6.0), (0.64, 5.0), (0.50, 5.0), (0.35, 3.0), (0.10, 1.0)],
)
def test_bands_return_the_first_band_the_value_reaches(value: float, expected: float) -> None:
    bands = ((0.65, 6.0), (0.50, 5.0), (0.40, 4.0), (0.30, 3.0), (0.20, 2.0), (0.10, 1.0))

    assert band(bands, value) == expected


@pytest.mark.unit
def test_bands_fall_back_to_the_default_below_every_bound() -> None:
    assert band(((0.10, 1.0),), 0.05) == 0.0
