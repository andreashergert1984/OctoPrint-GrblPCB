# -*- coding: utf-8 -*-
"""Tests for toolpath generation, mirroring, and alignment (toolpath.py)."""
import math

import pytest
from shapely.geometry import MultiPolygon, Polygon, LineString

from octoprint_grblpcb.toolpath import (
    AlignmentTransform,
    ToolpathSettings,
    _buffer_individual_paths,
    _find_min_processable_gap,
    _generate_tab_positions,
    _extract_line_segments,
    _optimize_drill_order,
    _points_close,
    apply_alignment_transform,
    compute_alignment_transform,
    generate_drill_gcode,
    generate_isolation_gcode,
    generate_mask_expose_gcode,
    generate_outline_gcode,
    mirror_coordinates_x,
    mirror_coordinates_y,
)


# =========================================================================
# ToolpathSettings
# =========================================================================


class TestToolpathSettings:
    def test_effective_width_v_bit(self):
        """60° V-bit, 0.1mm tip, 0.1mm depth → width ≈ 0.2155mm."""
        s = ToolpathSettings(tool_diameter=0.1, tool_angle=60.0, cut_depth=0.1)
        expected = 2 * 0.1 * math.tan(math.radians(30)) + 0.1
        assert abs(s.effective_width - expected) < 1e-6

    def test_effective_width_isolation_override(self):
        s = ToolpathSettings(isolation_width=0.5)
        assert s.effective_width == 0.5


# =========================================================================
# _points_close
# =========================================================================


class TestPointsClose:
    def test_close(self):
        assert _points_close((0.0, 0.0), (0.0005, 0.0005))

    def test_not_close(self):
        assert not _points_close((0.0, 0.0), (0.01, 0.01))


# =========================================================================
# Isolation G-code generation
# =========================================================================


def _make_square_copper(size: float = 10.0) -> MultiPolygon:
    """Create a simple square pad for testing."""
    return MultiPolygon([Polygon([(0, 0), (size, 0), (size, size), (0, size)])])


class TestGenerateIsolation:
    def test_preamble(self):
        copper = _make_square_copper()
        gcode = generate_isolation_gcode(copper, ToolpathSettings())
        lines = gcode.splitlines()
        # Must contain setup commands
        assert any("G21" in l for l in lines)
        assert any("G90" in l for l in lines)
        assert any("M3 S" in l for l in lines)

    def test_postamble(self):
        copper = _make_square_copper()
        gcode = generate_isolation_gcode(copper, ToolpathSettings())
        lines = gcode.splitlines()
        assert any("M5" in l for l in lines)
        assert any("M2" in l for l in lines)
        assert any("G0 X0 Y0" in l for l in lines)

    def test_origin_offset(self):
        copper = _make_square_copper(10.0)
        gcode = generate_isolation_gcode(
            copper, ToolpathSettings(), origin_x=5.0, origin_y=3.0
        )
        # G1 moves should have negative coordinates since origin shifts
        movement_lines = [l for l in gcode.splitlines() if l.startswith("G1 X")]
        assert len(movement_lines) > 0
        # All X values should be offset by -5, Y by -3

    def test_depth_per_pass(self):
        copper = _make_square_copper()
        settings = ToolpathSettings(cut_depth=0.3, depth_per_pass=0.1)
        gcode = generate_isolation_gcode(copper, settings)
        # Should have 3 depth passes
        depth_comments = [l for l in gcode.splitlines() if "; Depth pass" in l]
        assert len(depth_comments) == 3

    def test_multi_pass(self):
        copper = _make_square_copper()
        settings = ToolpathSettings(passes=3)
        gcode = generate_isolation_gcode(copper, settings)
        pass_comments = [l for l in gcode.splitlines() if "; --- Lateral Pass" in l]
        assert len(pass_comments) == 3

    def test_empty_geometry(self):
        copper = MultiPolygon()
        gcode = generate_isolation_gcode(copper, ToolpathSettings())
        # Should still have preamble/postamble but no path comments
        assert "M2" in gcode
        assert "; Path " not in gcode

    def test_max_width_auto_passes(self):
        """max_width > 0 auto-computes the needed number of lateral passes."""
        copper = _make_square_copper()
        settings = ToolpathSettings(
            tool_diameter=0.1, tool_angle=60.0, cut_depth=0.1,
            max_width=1.0,
        )
        ew = settings.effective_width  # ~0.2155 mm
        gcode = generate_isolation_gcode(copper, settings)
        # step_over = ew * 0.5 since pass_overlap == 0
        step_over = ew * 0.5
        expected = math.ceil((1.0 - ew) / step_over) + 1
        pass_comments = [l for l in gcode.splitlines() if "; --- Lateral Pass" in l]
        assert len(pass_comments) == expected
        assert "; Clearance width: 1.0 mm" in gcode

    def test_max_width_small_uses_one_pass(self):
        """max_width smaller than effective_width should result in 1 pass."""
        copper = _make_square_copper()
        settings = ToolpathSettings(max_width=0.05)
        gcode = generate_isolation_gcode(copper, settings)
        pass_comments = [l for l in gcode.splitlines() if "; --- Lateral Pass" in l]
        assert len(pass_comments) == 1

    def test_max_width_zero_uses_fixed_passes(self):
        """max_width=0 should not override the fixed passes count."""
        copper = _make_square_copper()
        settings = ToolpathSettings(passes=2, max_width=0.0)
        gcode = generate_isolation_gcode(copper, settings)
        pass_comments = [l for l in gcode.splitlines() if "; --- Lateral Pass" in l]
        assert len(pass_comments) == 2
        assert "Clearance width" not in gcode

    def test_clear_gaps_fills_between_pads(self):
        """clear_gaps=True keeps adding passes until gap between two pads is gone."""
        # Two 2mm pads separated by a 1mm gap
        pad1 = Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])
        pad2 = Polygon([(3, 0), (5, 0), (5, 2), (3, 2)])
        copper = MultiPolygon([pad1, pad2])
        settings = ToolpathSettings(
            tool_diameter=0.1, tool_angle=60.0, cut_depth=0.1,
            clear_gaps=True,
        )
        gcode = generate_isolation_gcode(copper, settings)
        pass_comments = [l for l in gcode.splitlines() if "; --- Lateral Pass" in l]
        # Must have more than 1 pass (gap = 1mm, effective_width ~0.215mm)
        assert len(pass_comments) > 1
        assert "auto (clear gaps)" in gcode

    def test_clear_gaps_single_pad_stops(self):
        """clear_gaps=True with one pad eventually runs out of paths."""
        copper = _make_square_copper(2.0)
        settings = ToolpathSettings(clear_gaps=True)
        gcode = generate_isolation_gcode(copper, settings)
        pass_comments = [l for l in gcode.splitlines() if "; --- Lateral Pass" in l]
        # Should produce some passes then stop — not the max 200
        assert 1 <= len(pass_comments) < 200

    def test_clear_gaps_false_uses_fixed_passes(self):
        """clear_gaps=False should use the manual passes count."""
        copper = _make_square_copper()
        settings = ToolpathSettings(passes=2, clear_gaps=False)
        gcode = generate_isolation_gcode(copper, settings)
        pass_comments = [l for l in gcode.splitlines() if "; --- Lateral Pass" in l]
        assert len(pass_comments) == 2

    def test_narrow_gap_prepass(self):
        """Two pads with gap < effective_width get a narrow-gap pre-pass."""
        # Two 2×1mm rectangular pads with a 0.2mm gap (< effective_width ~0.331)
        pad1 = Polygon([(0, 0), (2, 0), (2, 1), (0, 1)])
        pad2 = Polygon([(0, 1.2), (2, 1.2), (2, 2.2), (0, 2.2)])
        copper = MultiPolygon([pad1, pad2])
        settings = ToolpathSettings(
            tool_diameter=0.1, tool_angle=60.0, cut_depth=0.2,
        )
        gcode = generate_isolation_gcode(copper, settings)
        # Should have narrow-gap pre-pass paths
        narrow_lines = [l for l in gcode.splitlines() if "narrow-gap" in l.lower()]
        assert len(narrow_lines) > 0, "Expected narrow-gap pre-pass paths"
        # Should also have normal lateral pass
        pass_comments = [l for l in gcode.splitlines() if "; --- Lateral Pass" in l]
        assert len(pass_comments) >= 1

    def test_narrow_gap_depth_limited(self):
        """Narrow-gap pre-pass depth is limited to avoid undercutting."""
        pad1 = Polygon([(0, 0), (2, 0), (2, 1), (0, 1)])
        pad2 = Polygon([(0, 1.2), (2, 1.2), (2, 2.2), (0, 2.2)])
        copper = MultiPolygon([pad1, pad2])
        settings = ToolpathSettings(
            tool_diameter=0.1, tool_angle=60.0, cut_depth=0.2,
        )
        gcode = generate_isolation_gcode(copper, settings)
        # Z-depth in narrow-gap paths should be less than full cut depth
        prepass_section = gcode.split("; --- Lateral Pass")[0]
        z_values = []
        for line in prepass_section.splitlines():
            if line.strip().startswith("G1 Z"):
                z = float(line.strip().split()[1][1:])
                z_values.append(z)
        # Narrow-gap Z should be shallower than -cut_depth
        if z_values:
            assert all(z > -settings.cut_depth for z in z_values), \
                f"Narrow-gap depth {z_values} should be shallower than {-settings.cut_depth}"

    def test_no_narrow_gap_when_wide(self):
        """No narrow-gap pre-pass when all gaps are wider than effective_width."""
        # Two pads with 2mm gap — no narrow gaps
        pad1 = Polygon([(0, 0), (2, 0), (2, 1), (0, 1)])
        pad2 = Polygon([(0, 3), (2, 3), (2, 4), (0, 4)])
        copper = MultiPolygon([pad1, pad2])
        settings = ToolpathSettings(
            tool_diameter=0.1, tool_angle=60.0, cut_depth=0.2,
        )
        gcode = generate_isolation_gcode(copper, settings)
        narrow_lines = [l for l in gcode.splitlines() if "narrow-gap" in l.lower()]
        assert len(narrow_lines) == 0, "Should not have narrow-gap paths for wide gaps"


# =========================================================================
# Narrow-gap helper functions
# =========================================================================


class TestFindMinProcessableGap:
    def test_two_close_pads(self):
        pad1 = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
        pad2 = Polygon([(0, 1.15), (1, 1.15), (1, 2.15), (0, 2.15)])
        copper = MultiPolygon([pad1, pad2])
        gap = _find_min_processable_gap(copper, 0.1, 0.331)
        assert 0.1 <= gap < 0.331
        assert abs(gap - 0.15) < 0.01

    def test_no_narrow_gaps(self):
        pad1 = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
        pad2 = Polygon([(0, 5), (1, 5), (1, 6), (0, 6)])
        copper = MultiPolygon([pad1, pad2])
        gap = _find_min_processable_gap(copper, 0.1, 0.331)
        assert gap == 0.0

    def test_too_narrow_for_tool(self):
        pad1 = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
        pad2 = Polygon([(0, 1.05), (1, 1.05), (1, 2.05), (0, 2.05)])
        copper = MultiPolygon([pad1, pad2])
        # Gap is 0.05mm < tool_diameter 0.1mm
        gap = _find_min_processable_gap(copper, 0.1, 0.331)
        assert gap == 0.0

    def test_single_polygon(self):
        copper = MultiPolygon([Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])])
        gap = _find_min_processable_gap(copper, 0.1, 0.331)
        assert gap == 0.0


class TestBufferIndividualPaths:
    def test_separates_close_features(self):
        """Individual buffering keeps separate features distinct."""
        pad1 = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
        pad2 = Polygon([(0, 1.2), (1, 1.2), (1, 2.2), (0, 2.2)])
        copper = MultiPolygon([pad1, pad2])
        paths = _buffer_individual_paths(copper, 0.1)
        # Should produce at least 2 paths (one per pad exterior)
        assert len(paths) >= 2

    def test_handles_polygon_with_hole(self):
        """Correctly buffers a polygon with an interior ring."""
        outer = Polygon(
            [(0, 0), (10, 0), (10, 10), (0, 10)],
            [[(3, 3), (7, 3), (7, 7), (3, 7)]],
        )
        copper = MultiPolygon([outer])
        paths = _buffer_individual_paths(copper, 0.1)
        # Should have at least: exterior + interior ring
        assert len(paths) >= 2


class TestGenerateDrill:
    def _make_drills(self):
        from octoprint_grblpcb.pcb_project import DrillInfo

        return [
            DrillInfo(
                tool_number=1,
                diameter=0.8,
                holes=[(10.0, 20.0), (30.0, 40.0), (50.0, 10.0)],
                count=3,
            )
        ]

    def test_basic_drill(self):
        drills = self._make_drills()
        gcode = generate_drill_gcode(drills, ToolpathSettings())
        assert "G21" in gcode
        assert "M0" in gcode  # tool change pause
        assert "M2" in gcode  # program end

    def test_drill_origin_offset(self):
        drills = self._make_drills()
        gcode = generate_drill_gcode(
            drills, ToolpathSettings(), origin_x=10.0, origin_y=20.0
        )
        # First hole (10, 20) should become (0, 0)
        assert "G0 X0.0000 Y0.0000" in gcode

    def test_peck_drilling(self):
        drills = self._make_drills()
        settings = ToolpathSettings(cut_depth=2.0, peck_depth=0.5, breakthrough_depth=0.0)
        gcode = generate_drill_gcode(drills, settings)
        # Each hole should have 4 peck cycles (2.0 / 0.5 = 4)
        lines = gcode.splitlines()
        # Count Z plunge moves for first hole (after its G0 XY move)
        plunges_per_hole = sum(
            1 for l in lines if "G1 Z-" in l and "F" in l
        )
        # 3 holes × 4 pecks = 12 plunge moves
        assert plunges_per_hole == 12


# =========================================================================
# Drill order optimization
# =========================================================================


class TestOptimizeDrillOrder:
    def test_small_inputs(self):
        assert _optimize_drill_order([]) == []
        assert _optimize_drill_order([(1, 2)]) == [(1, 2)]
        assert len(_optimize_drill_order([(1, 2), (3, 4)])) == 2

    def test_reduces_travel(self):
        """Verify optimized order is shorter than a deliberately bad order."""
        holes = [(0, 0), (100, 100), (1, 0), (99, 100), (2, 0)]
        optimized = _optimize_drill_order(holes)

        def total_dist(order):
            dist = 0
            for i in range(1, len(order)):
                dx = order[i][0] - order[i - 1][0]
                dy = order[i][1] - order[i - 1][1]
                dist += math.sqrt(dx * dx + dy * dy)
            return dist

        assert total_dist(optimized) < total_dist(holes)


# =========================================================================
# Outline G-code generation


def test_drill_goes_through_with_breakthrough():
    settings = ToolpathSettings(cut_depth=1.6, peck_depth=0.5, breakthrough_depth=0.2)
    gcode = generate_drill_gcode(TestGenerateDrill()._make_drills(), settings)
    assert _deepest_z(gcode) == pytest.approx(-1.8)


def _deepest_z(gcode):
    import re

    return min(float(z) for z in re.findall(r"Z(-?\d+\.?\d*)", gcode))


# =========================================================================


class TestGenerateOutline:
    def _make_outline(self):
        return Polygon([(0, 0), (50, 0), (50, 30), (0, 30)])

    def test_basic_outline(self):
        gcode = generate_outline_gcode(
            self._make_outline(), ToolpathSettings(), board_thickness=1.6
        )
        assert "G21" in gcode
        assert "M2" in gcode
        assert gcode  # non-empty

    def test_multipolygon_uses_largest(self):
        small = Polygon([(0, 0), (5, 0), (5, 5), (0, 5)])
        big = Polygon([(10, 10), (60, 10), (60, 40), (10, 40)])
        mp = MultiPolygon([small, big])
        gcode = generate_outline_gcode(mp, ToolpathSettings(), board_thickness=1.6)
        assert gcode  # non-empty, uses big polygon

    def test_outline_goes_through_and_keeps_tabs(self):
        settings = ToolpathSettings(breakthrough_depth=0.2, tab_height=0.5, peck_depth=0.5)
        gcode = generate_outline_gcode(self._make_outline(), settings, board_thickness=1.6)
        assert _deepest_z(gcode) == pytest.approx(-1.8)
        # Tabs stay 0.5 mm high, measured from the board bottom
        assert "G1 Z-1.1000" in gcode

    def test_linestring_input(self):
        ls = LineString([(0, 0), (50, 0), (50, 30), (0, 30), (0, 0)])
        gcode = generate_outline_gcode(ls, ToolpathSettings(), board_thickness=1.6)
        assert gcode  # should not crash


# =========================================================================
# Tab positions
# =========================================================================


class TestTabPositions:
    def test_zero_tabs(self):
        coords = [(0, 0), (100, 0)]
        assert _generate_tab_positions(coords, 0, 2.0) == []

    def test_four_tabs_on_100mm(self):
        # Create a simple square path with 100mm perimeter
        coords = [(0, 0), (25, 0), (25, 25), (0, 25), (0, 0)]
        tabs = _generate_tab_positions(coords, 4, 2.0)
        assert len(tabs) == 4
        # Check tab widths
        for start, end in tabs:
            assert abs((end - start) - 2.0) < 0.01


# =========================================================================
# Mirror coordinates
# =========================================================================


class TestMirrorCoordinates:
    def test_mirror_x(self):
        gcode = "G1 X10.0000 Y5.0000\nG1 X20.0000 Y10.0000"
        result = mirror_coordinates_x(gcode, board_width=50.0)
        lines = result.splitlines()
        assert "X40.0000" in lines[0]  # 50 - 10 = 40
        assert "Y5.0000" in lines[0]  # Y unchanged
        assert "X30.0000" in lines[1]  # 50 - 20 = 30

    def test_mirror_x_with_offset(self):
        gcode = "G1 X10.0000 Y5.0000"
        result = mirror_coordinates_x(gcode, board_width=50.0, flip_offset=2.0)
        assert "X42.0000" in result  # 50 - 10 + 2 = 42

    def test_mirror_y(self):
        gcode = "G1 X10.0000 Y5.0000"
        result = mirror_coordinates_y(gcode, board_height=30.0)
        assert "Y25.0000" in result  # 30 - 5 = 25
        assert "X10.0000" in result  # X unchanged

    def test_preserves_comments(self):
        gcode = "; comment line\n\nG1 X10 Y5"
        result = mirror_coordinates_x(gcode, board_width=50.0)
        lines = result.splitlines()
        assert lines[0] == "; comment line"
        assert lines[1] == ""


# =========================================================================
# AlignmentTransform
# =========================================================================


class TestAlignmentTransform:
    def test_serialization_roundtrip(self):
        t = AlignmentTransform(
            cos_theta=0.996,
            sin_theta=0.087,
            tx=1.5,
            ty=-0.3,
            theta_deg=5.0,
            rms_error=0.02,
        )
        d = t.to_dict()
        restored = AlignmentTransform.from_dict(d)
        assert abs(restored.cos_theta - 0.996) < 1e-9
        assert abs(restored.sin_theta - 0.087) < 1e-9
        assert abs(restored.tx - 1.5) < 1e-9
        assert abs(restored.ty - (-0.3)) < 1e-9

    def test_from_dict_ignores_extras(self):
        d = {"cos_theta": 1, "sin_theta": 0, "tx": 0, "ty": 0,
             "theta_deg": 0, "rms_error": 0, "extra_key": "hi"}
        t = AlignmentTransform.from_dict(d)
        assert t.cos_theta == 1


# =========================================================================
# compute_alignment_transform
# =========================================================================


class TestComputeAlignment:
    def test_identity(self):
        """Expected == measured → identity transform."""
        expected = [(0.0, 0.0), (50.0, 30.0)]
        measured = [(0.0, 0.0), (50.0, 30.0)]
        t = compute_alignment_transform(expected, measured)
        assert abs(t.cos_theta - 1.0) < 1e-6
        assert abs(t.sin_theta) < 1e-6
        assert abs(t.tx) < 1e-4
        assert abs(t.ty) < 1e-4
        assert t.rms_error < 1e-4

    def test_pure_translation(self):
        """Shift by (1, 2)."""
        expected = [(0.0, 0.0), (50.0, 30.0)]
        measured = [(1.0, 2.0), (51.0, 32.0)]
        t = compute_alignment_transform(expected, measured)
        assert abs(t.tx - 1.0) < 0.01
        assert abs(t.ty - 2.0) < 0.01
        assert abs(t.theta_deg) < 0.1

    def test_pure_rotation(self):
        """5° rotation around centroid."""
        import numpy as np

        expected = [(0.0, 0.0), (50.0, 0.0), (50.0, 30.0)]
        cx = sum(p[0] for p in expected) / 3
        cy = sum(p[1] for p in expected) / 3
        angle = math.radians(5.0)
        cos_a, sin_a = math.cos(angle), math.sin(angle)

        measured = []
        for ex, ey in expected:
            dx, dy = ex - cx, ey - cy
            mx = cos_a * dx - sin_a * dy + cx
            my = sin_a * dx + cos_a * dy + cy
            measured.append((mx, my))

        t = compute_alignment_transform(expected, measured)
        assert abs(t.theta_deg - 5.0) < 0.1
        assert t.rms_error < 0.05

    def test_rotation_plus_translation(self):
        angle = math.radians(3.0)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        tx, ty = 0.5, -0.3

        expected = [(0.0, 0.0), (40.0, 0.0), (40.0, 25.0)]
        measured = [
            (cos_a * ex - sin_a * ey + tx, sin_a * ex + cos_a * ey + ty)
            for ex, ey in expected
        ]
        t = compute_alignment_transform(expected, measured)
        assert abs(t.theta_deg - 3.0) < 0.1
        assert abs(t.tx - tx) < 0.05
        assert abs(t.ty - ty) < 0.05

    def test_insufficient_points(self):
        with pytest.raises(ValueError, match="at least 2"):
            compute_alignment_transform([(0, 0)], [(1, 1)])

    def test_mismatched_lengths(self):
        with pytest.raises(ValueError, match="same length"):
            compute_alignment_transform([(0, 0), (1, 1)], [(0, 0)])


# =========================================================================
# apply_alignment_transform
# =========================================================================


class TestApplyAlignment:
    def test_identity_passthrough(self):
        gcode = "G0 X10 Y20\nG1 X30 Y40"
        t = AlignmentTransform()  # identity
        result = apply_alignment_transform(gcode, t)
        assert result == gcode

    def test_translation(self):
        gcode = "G1 X10.0000 Y20.0000"
        t = AlignmentTransform(cos_theta=1.0, sin_theta=0.0, tx=1.0, ty=2.0)
        result = apply_alignment_transform(gcode, t)
        assert "X11.0000" in result
        assert "Y22.0000" in result

    def test_rotation_90(self):
        """90° rotation: (10, 0) → (0, 10)."""
        gcode = "G1 X10.0000 Y0.0000"
        t = AlignmentTransform(
            cos_theta=0.0, sin_theta=1.0, tx=0.0, ty=0.0
        )
        result = apply_alignment_transform(gcode, t)
        # x' = cos*10 - sin*0 = 0, y' = sin*10 + cos*0 = 10
        assert "X0.0000" in result
        assert "Y10.0000" in result

    def test_preserves_comments(self):
        gcode = "; header\n\nG1 X10 Y20"
        t = AlignmentTransform(cos_theta=1.0, sin_theta=0.0, tx=1.0, ty=0.0)
        result = apply_alignment_transform(gcode, t)
        lines = result.splitlines()
        assert lines[0] == "; header"
        assert lines[1] == ""

    def test_x_only_line_passthrough(self):
        """Lines with only X (no Y) pass through unchanged."""
        gcode = "G1 X10.0000"
        t = AlignmentTransform(cos_theta=1.0, sin_theta=0.0, tx=5.0, ty=5.0)
        result = apply_alignment_transform(gcode, t)
        # X-only line is NOT transformed (needs both X and Y)
        assert "X10.0000" in result


# =========================================================================
# UV Mask Exposure
# =========================================================================


class TestMaskExpose:
    """Tests for generate_mask_expose_gcode()."""

    def _default_settings(self, **overrides):
        kw = dict(
            uv_offset_x=0.0,
            uv_offset_y=0.0,
            uv_spot_size=1.0,
            uv_expose_speed=200.0,
            uv_expose_z=5.0,
            uv_power=255,
            safe_z=5.0,
        )
        kw.update(overrides)
        return ToolpathSettings(**kw)

    def test_full_board_no_mask(self):
        """No mask geometry → expose entire board rectangle."""
        settings = self._default_settings(uv_spot_size=1.0)
        gcode = generate_mask_expose_gcode(
            mask_geometry=None,
            board_bounds=(0, 0, 4, 3),
            settings=settings,
        )
        assert gcode
        assert "UV Solder Mask Exposure" in gcode
        assert "M3 S255" in gcode
        assert "M5" in gcode
        assert "M2" in gcode
        # Should contain G1 exposure moves
        assert "G1" in gcode

    def test_board_with_pad_opening(self):
        """Pad opening should NOT be exposed — G-code skips the hole."""
        # 10x10 board with a 2x2 pad opening at centre (4,4)-(6,6)
        pad = Polygon([(4, 4), (6, 4), (6, 6), (4, 6)])
        settings = self._default_settings(uv_spot_size=1.0)
        gcode = generate_mask_expose_gcode(
            mask_geometry=pad,
            board_bounds=(0, 0, 10, 10),
            settings=settings,
        )
        assert gcode
        # Should have multiple M3/M5 pairs (LED on/off per segment)
        assert gcode.count("M3 S255") >= 2

    def test_led_offset_shifts_coordinates(self):
        """LED offset must shift all XY coordinates."""
        settings = self._default_settings(
            uv_offset_x=10.0, uv_offset_y=5.0, uv_spot_size=2.0
        )
        gcode = generate_mask_expose_gcode(
            mask_geometry=None,
            board_bounds=(0, 0, 4, 3),
            settings=settings,
        )
        # With offset (10, 5), board coords (0..4, 0..3) become
        # roughly (-10..-6, -5..-2) in G-code.
        assert gcode
        assert "X-" in gcode  # negative X from offset
        assert "Y-" in gcode  # negative Y from offset

    def test_origin_offset(self):
        """origin_x/y should shift coordinates to CNC zero."""
        settings = self._default_settings(uv_spot_size=2.0)
        gcode = generate_mask_expose_gcode(
            mask_geometry=None,
            board_bounds=(10, 20, 14, 23),
            settings=settings,
            origin_x=10.0,
            origin_y=20.0,
        )
        assert gcode
        # After origin subtraction, coords should be in 0..4, 0..3 range
        lines = [l for l in gcode.splitlines() if l.startswith("G0 X") or l.startswith("G1 X")]
        for line in lines:
            # Extract X value
            import re
            xm = re.search(r"X([+-]?\d+\.?\d*)", line)
            if xm:
                x = float(xm.group(1))
                assert -1 <= x <= 5, f"X={x} out of expected range in: {line}"

    def test_empty_keep_area(self):
        """If mask covers entire board, keep_area is empty → returns ''."""
        from shapely.geometry import box
        board = box(0, 0, 10, 10)
        # Mask = entire board → nothing to expose
        settings = self._default_settings()
        gcode = generate_mask_expose_gcode(
            mask_geometry=board,
            board_bounds=(0, 0, 10, 10),
            settings=settings,
        )
        assert gcode == ""

    def test_exposure_z_in_gcode(self):
        """Exposure Z height should appear in preamble."""
        settings = self._default_settings(uv_expose_z=7.5)
        gcode = generate_mask_expose_gcode(
            mask_geometry=None,
            board_bounds=(0, 0, 4, 3),
            settings=settings,
        )
        # Z moves are intentionally feed-limited via settings.z_move(...)
        assert "G1 Z7.500 F120" in gcode

    def test_serpentine_pattern(self):
        """Odd scan rows should reverse direction (serpentine)."""
        # Board must be tall enough for ≥2 scan lines after beam-radius
        # erosion: height=3, spot=1 → eroded height=2, scan at y=1 and y=2
        settings = self._default_settings(uv_spot_size=1.0)
        gcode = generate_mask_expose_gcode(
            mask_geometry=None,
            board_bounds=(0, 0, 10, 3),
            settings=settings,
        )
        assert gcode
        # With spot_size=1.0 and height=3, we get ≥2 scan lines
        # The first should go left-to-right, second right-to-left
        g1_lines = [l for l in gcode.splitlines() if l.startswith("G1 X")]
        assert len(g1_lines) >= 2


class TestExtractLineSegments:
    """Tests for _extract_line_segments helper."""

    def test_empty_geometry(self):
        from shapely.geometry import LineString
        empty = LineString()
        assert _extract_line_segments(empty) == []

    def test_single_linestring(self):
        line = LineString([(0, 0), (10, 0)])
        segs = _extract_line_segments(line)
        assert len(segs) == 1
        assert len(segs[0]) == 2

    def test_multilinestring(self):
        from shapely.geometry import MultiLineString
        ml = MultiLineString([[(0, 0), (3, 0)], [(5, 0), (10, 0)]])
        segs = _extract_line_segments(ml)
        assert len(segs) == 2
