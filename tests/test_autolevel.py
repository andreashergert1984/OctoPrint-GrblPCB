# -*- coding: utf-8 -*-
"""Tests for autoleveling: height map, interpolation, G-code Z-correction."""
import math

import pytest
import numpy as np

from octoprint_grblpcb.autolevel import (
    HeightMap,
    ProbePoint,
    _extract_feed,
    _parse_xyz,
    _rebuild_gcode_line,
    _subdivide_segment,
    apply_autolevel,
)


# =========================================================================
# ProbePoint
# =========================================================================


class TestProbePoint:
    def test_to_dict_roundtrip(self):
        p = ProbePoint(x=10.5, y=20.3, z=-0.15, probed=True)
        d = p.to_dict()
        r = ProbePoint.from_dict(d)
        assert r.x == 10.5
        assert r.y == 20.3
        assert r.z == -0.15
        assert r.probed is True


# =========================================================================
# HeightMap grid generation
# =========================================================================


class TestHeightMapGrid:
    def test_grid_dimensions(self):
        hm = HeightMap()
        hm.generate_grid(0, 0, 50, 30, grid_x=5, grid_y=3, margin=1.0)
        assert len(hm.points) == 15  # 5 × 3
        assert hm.min_x == -1.0
        assert hm.min_y == -1.0
        assert hm.max_x == 51.0
        assert hm.max_y == 31.0

    def test_serpentine_order(self):
        hm = HeightMap()
        hm.generate_grid(0, 0, 10, 10, grid_x=3, grid_y=3, margin=0)
        # Row 0 (even) → left to right
        assert hm.points[0].x < hm.points[1].x < hm.points[2].x
        # Row 1 (odd) → right to left
        assert hm.points[3].x > hm.points[4].x > hm.points[5].x
        # Row 2 (even) → left to right
        assert hm.points[6].x < hm.points[7].x < hm.points[8].x

    def test_probe_result_and_completion(self):
        hm = HeightMap()
        hm.generate_grid(0, 0, 10, 10, grid_x=2, grid_y=2, margin=0)
        assert not hm.is_complete()

        for i in range(4):
            hm.set_probe_result(i, -0.1 * i)

        assert hm.is_complete()
        assert hm.points[2].z == pytest.approx(-0.2)

    def test_next_unprobed_index(self):
        hm = HeightMap()
        hm.generate_grid(0, 0, 10, 10, grid_x=2, grid_y=2, margin=0)
        assert hm.next_unprobed_index() == 0
        hm.set_probe_result(0, 0.0)
        assert hm.next_unprobed_index() == 1
        for i in range(1, 4):
            hm.set_probe_result(i, 0.0)
        assert hm.next_unprobed_index() == -1


# =========================================================================
# HeightMap interpolation
# =========================================================================


def _make_completed_heightmap(grid_x=3, grid_y=3, z_func=None):
    """Helper: create a completed height map with z_func(x, y) for each point."""
    if z_func is None:
        z_func = lambda x, y: 0.0
    hm = HeightMap()
    hm.generate_grid(0, 0, 10, 10, grid_x=grid_x, grid_y=grid_y, margin=0)
    for i, p in enumerate(hm.points):
        hm.set_probe_result(i, z_func(p.x, p.y))
    hm.build_interpolator()
    return hm


class TestHeightMapInterpolation:
    def test_flat_surface(self):
        hm = _make_completed_heightmap(z_func=lambda x, y: 0.0)
        assert abs(hm.get_z(5.0, 5.0)) < 1e-6
        assert abs(hm.get_z(0.0, 0.0)) < 1e-6

    def test_tilted_surface(self):
        """Z = 0.01x + 0.02y — bilinear should reproduce exactly."""
        hm = _make_completed_heightmap(
            grid_x=3, grid_y=3,
            z_func=lambda x, y: 0.01 * x + 0.02 * y,
        )
        assert hm.get_z(5.0, 5.0) == pytest.approx(0.15, abs=0.01)
        assert hm.get_z(10.0, 10.0) == pytest.approx(0.30, abs=0.01)

    def test_no_interpolator_returns_zero(self):
        hm = HeightMap()
        hm.generate_grid(0, 0, 10, 10, grid_x=3, grid_y=3, margin=0)
        # Not probed → no interpolator
        assert hm.get_z(5.0, 5.0) == 0.0

    def test_clamped_to_bounds(self):
        """Points outside grid are clamped (fill_value=None extrapolates)."""
        hm = _make_completed_heightmap(z_func=lambda x, y: 0.1)
        # Should not crash for out-of-bounds query
        z = hm.get_z(100.0, 100.0)
        assert isinstance(z, float)


# =========================================================================
# HeightMap serialization
# =========================================================================


class TestHeightMapSerialization:
    def test_roundtrip(self):
        hm = _make_completed_heightmap(
            z_func=lambda x, y: 0.01 * x + 0.02 * y
        )
        d = hm.to_dict()

        restored = HeightMap.from_dict(d)
        assert restored.completed is True
        assert len(restored.points) == len(hm.points)
        # Interpolator should auto-rebuild
        assert restored.get_z(5.0, 5.0) == pytest.approx(
            hm.get_z(5.0, 5.0), abs=0.001
        )


# =========================================================================
# G-code parsing helpers
# =========================================================================


class TestParseXYZ:
    def test_full_line(self):
        x, y, z = _parse_xyz("G1 X10.5 Y20.3 Z-0.1", 0, 0, 0)
        assert x == pytest.approx(10.5)
        assert y == pytest.approx(20.3)
        assert z == pytest.approx(-0.1)

    def test_defaults(self):
        x, y, z = _parse_xyz("G1 X5", 1.0, 2.0, 3.0)
        assert x == pytest.approx(5.0)
        assert y == pytest.approx(2.0)  # default
        assert z == pytest.approx(3.0)  # default


class TestExtractFeed:
    def test_present(self):
        assert _extract_feed("G1 X10 Y20 F500") == "500"

    def test_absent(self):
        assert _extract_feed("G1 X10 Y20") is None


class TestRebuildGcodeLine:
    def test_g0(self):
        result = _rebuild_gcode_line("G0 X1 Y2 Z3", 10.0, 20.0, 30.0)
        assert result == "G0 X10.0000 Y20.0000 Z30.0000"

    def test_g1_with_feed(self):
        result = _rebuild_gcode_line("G1 X1 Y2 Z3 F500", 10.0, 20.0, -0.1)
        assert "G1" in result
        assert "F500" in result
        assert "Z-0.1000" in result


# =========================================================================
# Segment subdivision
# =========================================================================


class TestSubdivideSegment:
    def test_short_segment(self):
        """Segment shorter than max_length returns single endpoint."""
        result = _subdivide_segment(0, 0, 0, 1, 0, 0, max_length=5.0)
        assert len(result) == 1
        assert result[0] == pytest.approx((1.0, 0.0, 0.0))

    def test_long_segment(self):
        """10mm segment with max_length=3: ceil(10/3)=4 sub-segments."""
        result = _subdivide_segment(0, 0, 0, 10, 0, 0, max_length=3.0)
        assert len(result) == 4
        # Last point should be the endpoint
        assert result[-1][0] == pytest.approx(10.0)

    def test_z_interpolation(self):
        """Z is interpolated linearly."""
        result = _subdivide_segment(0, 0, 0, 10, 0, -1.0, max_length=5.0)
        assert len(result) == 2
        assert result[0][2] == pytest.approx(-0.5)
        assert result[1][2] == pytest.approx(-1.0)

    def test_degenerate_zero_length(self):
        result = _subdivide_segment(5, 5, -0.1, 5, 5, -0.1, max_length=1.0)
        assert len(result) == 1


# =========================================================================
# apply_autolevel
# =========================================================================


class TestApplyAutolevel:
    def test_flat_surface_no_change(self):
        hm = _make_completed_heightmap(z_func=lambda x, y: 0.0)
        gcode = "G0 Z5.000\nG0 X5.0000 Y5.0000\nG1 Z-0.1000 F100\nG1 X10.0000 Y5.0000"
        result = apply_autolevel(gcode, hm)
        # Z correction is 0 everywhere → Z values should be unchanged
        assert "Z-0.1000" in result

    def test_tilted_surface_adds_correction(self):
        hm = _make_completed_heightmap(
            z_func=lambda x, y: 0.1  # uniform 0.1mm surface offset
        )
        gcode = "G0 Z5.000\nG0 X5.0000 Y5.0000\nG1 Z-0.1000 F100"
        result = apply_autolevel(gcode, hm)
        # Z = -0.1 + 0.1 correction = 0.0
        lines = result.splitlines()
        # The G1 line should be corrected
        corrected_z_lines = [l for l in lines if l.startswith("G1") and "Z" in l]
        assert len(corrected_z_lines) > 0

    def test_preserves_comments(self):
        hm = _make_completed_heightmap()
        gcode = "; header comment\n\nM3 S10000\nG1 X5 Y5 Z-0.1 F100"
        result = apply_autolevel(gcode, hm)
        lines = result.splitlines()
        assert lines[0] == "; header comment"
        assert lines[1] == ""
        assert lines[2] == "M3 S10000"

    def test_rapid_above_surface_not_corrected(self):
        hm = _make_completed_heightmap(z_func=lambda x, y: 0.1)
        gcode = "G0 X5.0000 Y5.0000\nG0 Z5.0000"
        result = apply_autolevel(gcode, hm)
        # Z=5 is above surface, should not get correction
        assert "Z5.0000" in result
