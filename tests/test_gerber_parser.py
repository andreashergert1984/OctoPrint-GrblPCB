"""Tests for gerber_parser module — aperture shape handling."""

import math
from unittest.mock import MagicMock

from octoprint_grblpcb.gerber_parser import (
    _aperture_radius,
    _obround_aperture_flash,
    _rectangle_aperture_flash,
)


class _Offset:
    """Minimal pygerber Offset mock."""
    def __init__(self, v):
        self.value = v
    def __float__(self):
        return float(self.value)
    def __repr__(self):
        return f"Offset({self.value})"


def _make_aperture(cls_name, **kwargs):
    """Build a mock aperture with the given attributes."""
    ap = MagicMock()
    type(ap).__name__ = cls_name
    for k, v in kwargs.items():
        setattr(ap, k, v)
    return ap


# ---- Rectangle2 tests -------------------------------------------------------


class TestRectangleAperture:
    def test_basic_rectangle(self):
        ap = _make_aperture("Rectangle2",
                            x_size=_Offset(2.0), y_size=_Offset(0.9),
                            rotation=0.0)
        geom = _rectangle_aperture_flash(ap, 10.0, 20.0)
        assert geom is not None
        assert not geom.is_empty
        # Check bounds
        bx0, by0, bx1, by1 = geom.bounds
        assert abs(bx0 - 9.0) < 1e-6
        assert abs(by0 - 19.55) < 1e-6
        assert abs(bx1 - 11.0) < 1e-6
        assert abs(by1 - 20.45) < 1e-6

    def test_square_rectangle(self):
        ap = _make_aperture("Rectangle2",
                            x_size=_Offset(1.7), y_size=_Offset(1.7),
                            rotation=0.0)
        geom = _rectangle_aperture_flash(ap, 0, 0)
        assert abs(geom.area - 1.7 * 1.7) < 1e-6

    def test_rotated_rectangle(self):
        ap = _make_aperture("Rectangle2",
                            x_size=_Offset(2.0), y_size=_Offset(1.0),
                            rotation=90.0)
        geom = _rectangle_aperture_flash(ap, 5.0, 5.0)
        bx0, by0, bx1, by1 = geom.bounds
        # 90° rotation swaps width and height
        assert abs((bx1 - bx0) - 1.0) < 1e-6
        assert abs((by1 - by0) - 2.0) < 1e-6

    def test_area_preserved(self):
        ap = _make_aperture("Rectangle2",
                            x_size=_Offset(3.0), y_size=_Offset(2.0),
                            rotation=0.0)
        geom = _rectangle_aperture_flash(ap, 0, 0)
        assert abs(geom.area - 6.0) < 1e-6


# ---- Obround2 tests ---------------------------------------------------------


class TestObroundAperture:
    def test_horizontal_obround(self):
        """Obround with x > y should be a horizontal capsule."""
        ap = _make_aperture("Obround2",
                            x_size=_Offset(2.0), y_size=_Offset(1.0),
                            rotation=0.0)
        geom = _obround_aperture_flash(ap, 0, 0)
        assert geom is not None
        bx0, by0, bx1, by1 = geom.bounds
        assert abs((bx1 - bx0) - 2.0) < 0.01
        assert abs((by1 - by0) - 1.0) < 0.01

    def test_vertical_obround(self):
        """Obround with y > x should be a vertical capsule."""
        ap = _make_aperture("Obround2",
                            x_size=_Offset(1.1), y_size=_Offset(1.7),
                            rotation=0.0)
        geom = _obround_aperture_flash(ap, 5, 5)
        bx0, by0, bx1, by1 = geom.bounds
        assert abs((bx1 - bx0) - 1.1) < 0.01
        assert abs((by1 - by0) - 1.7) < 0.01

    def test_square_obround_is_circle(self):
        """Equal x_size and y_size → circle."""
        ap = _make_aperture("Obround2",
                            x_size=_Offset(1.0), y_size=_Offset(1.0),
                            rotation=0.0)
        geom = _obround_aperture_flash(ap, 0, 0)
        expected_area = math.pi * (0.5 ** 2)
        assert abs(geom.area - expected_area) < 0.01

    def test_obround_area(self):
        """Area of a capsule = rectangle + circle."""
        w, h = 2.0, 1.0
        ap = _make_aperture("Obround2",
                            x_size=_Offset(w), y_size=_Offset(h),
                            rotation=0.0)
        geom = _obround_aperture_flash(ap, 0, 0)
        # Capsule area = (w-h)*h + pi*(h/2)^2
        r = h / 2
        expected = (w - h) * h + math.pi * r * r
        assert abs(geom.area - expected) < 0.02


# ---- Circle fallback ---------------------------------------------------------


class TestCircleAperture:
    def test_circle_via_diameter(self):
        ap = _make_aperture("Circle2", diameter=_Offset(1.7))
        r = _aperture_radius(ap)
        assert abs(r - 0.85) < 1e-6

    def test_fallback_x_size(self):
        ap = _make_aperture("Unknown", x_size=_Offset(2.0))
        del ap.diameter
        del ap.outer_diameter
        del ap.width
        r = _aperture_radius(ap)
        assert abs(r - 1.0) < 1e-6
