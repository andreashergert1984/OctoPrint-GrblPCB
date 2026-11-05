"""Geometric checks of the generated G-code (issues #22 to #27)."""
import math
import re

import pytest
from shapely.geometry import MultiPolygon, Point, Polygon, box

from gcode_reader import cutting_paths, moves, signed_area
from octoprint_grblpcb.pcb_project import DrillInfo
from octoprint_grblpcb.toolpath import (
    ToolpathSettings,
    board_shape_from_outline,
    find_unisolatable_gaps,
    generate_drill_gcode,
    generate_isolation_gcode,
    generate_outline_gcode,
)


def _stroke_ring(x0, y0, x1, y1, width=0.1):
    """Outline drawn as a stroke, as the Gerber parser returns Edge.Cuts."""
    centre = box(x0, y0, x1, y1)
    return centre.buffer(width / 2, join_style=2).difference(centre.buffer(-width / 2, join_style=2))


# ---------------------------------------------------------------- outline / tabs (#22)

def test_outline_cuts_through_except_at_tabs():
    s = ToolpathSettings(tool_diameter=2.0, num_tabs=4, tab_width=3.0, tab_height=0.5,
                         outline_step_down=0.5, breakthrough_depth=0.2, feed_rate=400, plunge_rate=100)
    g = generate_outline_gcode(box(0, 0, 100, 50), s, board_thickness=1.6)
    deepest = [m for m in moves(g) if not m["rapid"] and m["start"][2] == m["end"][2] == -1.8]
    cut = sum(math.hypot(m["end"][0] - m["start"][0], m["end"][1] - m["start"][1]) for m in deepest)
    tool_path = 2 * (102 + 52)
    assert cut == pytest.approx(tool_path - 4 * 3.0, abs=0.5)
    # tabs stay 0.5 mm thick, measured from the bottom of the board
    tab_moves = [m for m in moves(g) if m["start"][2] == m["end"][2] == -1.1 and not m["rapid"]]
    assert sum(math.hypot(m["end"][0] - m["start"][0], m["end"][1] - m["start"][1]) for m in tab_moves) \
        == pytest.approx(4 * 3.0 * 2, abs=0.5)  # two passes reach below the tab top
    # every cutting XY move runs at the cutting feed, never at the plunge feed
    xy = [m for m in moves(g) if not m["rapid"] and m["start"][2] < 0 and m["start"][:2] != m["end"][:2]]
    assert {m["feed"] for m in xy} == {400}


# ---------------------------------------------------------------- direction (#23)

def test_isolation_is_climb_milling_on_both_sides():
    s = ToolpathSettings(tool_diameter=0.1, tool_angle=60, cut_depth=0.1)
    copper = MultiPolygon([box(10, 10, 20, 15)])
    for kwargs in ({}, {"mirror": "X", "board_size": (50.0, 30.0)}):
        paths = cutting_paths(generate_isolation_gcode(copper, s, **kwargs))
        assert paths and all(signed_area(p) > 0 for p in paths), kwargs


def test_copper_hole_is_milled_clockwise():
    s = ToolpathSettings(tool_diameter=0.1, tool_angle=60, cut_depth=0.1)
    ring = Polygon(box(0, 0, 20, 20).exterior.coords, holes=[box(5, 5, 15, 15).exterior.coords])
    areas = sorted(signed_area(p) for p in cutting_paths(generate_isolation_gcode(MultiPolygon([ring]), s)))
    assert areas[0] < 0 < areas[-1]  # hole clockwise, outside counter-clockwise


def test_mirrored_isolation_lands_on_mirrored_position():
    s = ToolpathSettings(tool_diameter=0.1, tool_angle=60, cut_depth=0.1)
    pad = MultiPolygon([box(9, 9, 11, 11)])
    paths = cutting_paths(generate_isolation_gcode(pad, s, origin_x=0, origin_y=0, mirror="X", board_size=(50.0, 30.0)))
    xs = [x for p in paths for x, _ in p]
    assert (min(xs) + max(xs)) / 2 == pytest.approx(40.0, abs=0.01)


def test_mirrored_drilling_matches_mirrored_isolation():
    drills = [DrillInfo(tool_number=1, diameter=0.8, holes=[(10.0, 10.0)], count=1)]
    g = generate_drill_gcode(drills, ToolpathSettings(cut_depth=1.6), mirror="X", board_size=(50.0, 30.0))
    assert "G0 X40.0000 Y10.0000" in g


# ---------------------------------------------------------------- overlap (#24)

@pytest.mark.parametrize("overlap", [10, 40, 75])
def test_neighbouring_passes_overlap_by_the_setting(overlap):
    s = ToolpathSettings(tool_diameter=0.1, tool_angle=60, cut_depth=0.1, passes=3, pass_overlap=overlap)
    g = generate_isolation_gcode(MultiPolygon([box(10, 10, 20, 15)]), s)
    offsets = [float(v) for v in re.findall(r"Lateral Pass \d+ \(offset ([\d.]+) mm\)", g)]
    shared = s.effective_width - (offsets[1] - offsets[0])
    assert shared / s.effective_width == pytest.approx(overlap / 100, abs=0.01)


def test_overlap_is_clamped_so_passes_always_overlap():
    s = ToolpathSettings(tool_diameter=0.1, tool_angle=60, cut_depth=0.1, pass_overlap=0)
    assert s.step_over < s.effective_width


# ---------------------------------------------------------------- drilling (#25)

def test_tool_changes_are_safe_and_sorted():
    drills = [
        DrillInfo(tool_number=2, diameter=1.0, holes=[(5, 5)], count=1),
        DrillInfo(tool_number=1, diameter=0.8, holes=[(1, 1), (2, 2)], count=2),
    ]
    lines = [ln.split(";")[0].strip() for ln in generate_drill_gcode(drills, ToolpathSettings(cut_depth=1.6, safe_z=5)).splitlines()]
    lines = [ln for ln in lines if ln]
    changes = [i for i, ln in enumerate(lines) if re.search(r"\bM6\b", ln)]
    assert [lines[i] for i in changes] == ["T1 M6", "T2 M6"]  # small drill first
    for i in changes:
        assert lines[i - 2].startswith("G1 Z5.000") and lines[i - 1] == "M5"
        assert lines[i + 1].startswith("M3") and lines[i + 2].startswith("G4")
    # no spindle before the first tool is in
    assert not any(ln.startswith("M3") for ln in lines[:changes[0]])


# ---------------------------------------------------------------- unisolatable gaps (#26)

def test_gap_narrower_than_tool_is_reported():
    copper = MultiPolygon([box(0, 0, 5, 5), box(5.05, 0, 10, 5)])
    gaps = find_unisolatable_gaps(copper, 0.1)
    assert len(gaps) == 1 and gaps[0][0] == pytest.approx(0.05)
    g = generate_isolation_gcode(copper, ToolpathSettings(tool_diameter=0.1, tool_angle=60, cut_depth=0.1))
    assert "WARNING: gap 0.050 mm" in g


def test_wide_enough_gaps_are_not_reported():
    assert find_unisolatable_gaps(MultiPolygon([box(0, 0, 5, 5), box(5.15, 0, 10, 5)]), 0.1) == []


# ---------------------------------------------------------------- outline shape and cutouts (#27)

def test_board_shape_uses_stroke_centre_line():
    board = board_shape_from_outline(MultiPolygon([_stroke_ring(0, 0, 100, 50, width=0.2)]))
    assert board.bounds == pytest.approx((0, 0, 100, 50), abs=0.01)


def test_cutout_is_milled_before_the_outer_contour():
    outline = MultiPolygon([_stroke_ring(0, 0, 100, 50), _stroke_ring(40, 20, 60, 30)])
    board = board_shape_from_outline(outline)
    assert board.area == pytest.approx(100 * 50 - 20 * 10, abs=1)
    s = ToolpathSettings(tool_diameter=2.0, num_tabs=0, outline_step_down=2.0)
    paths = cutting_paths(generate_outline_gcode(outline, s, board_thickness=1.6))
    inner, outer = paths[0], paths[-1]
    # tool stays inside the cutout (1 mm from its edges) and outside the board
    xs = [x for x, _ in inner]
    assert (min(xs), max(xs)) == pytest.approx((41.0, 59.0), abs=0.01)
    xs = [x for x, _ in outer]
    assert (min(xs), max(xs)) == pytest.approx((-1.0, 101.0), abs=0.01)
    assert signed_area(inner) < 0 < signed_area(outer)  # climb on both
    assert Point(50, 25).within(Polygon(inner))
