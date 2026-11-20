"""Regression tests for CAM review findings (parser, toolpath, autolevel)."""
import math

import pytest
from gcode_reader import cutting_paths, moves
from shapely.geometry import LineString, MultiPolygon, Point, box
from shapely.ops import unary_union

from octoprint_grblpcb.autolevel import HeightMap, apply_autolevel
from octoprint_grblpcb.gerber_parser import (
    merge_geometries,
    parse_excellon_file,
    parse_gerber_file,
)
from octoprint_grblpcb.pcb_project import LayerType, PCBProject
from octoprint_grblpcb.toolpath import (
    ToolpathSettings,
    compute_alignment_transform,
    generate_isolation_gcode,
    generate_mask_expose_gcode,
    generate_outline_gcode,
)

HEAD = "%FSLAX46Y46*%\n%MOMM*%\n"


def _gerber(tmp_path, body, head=HEAD):
    path = tmp_path / "t.gbr"
    path.write_text(head + body + "M02*\n")
    layer, geoms = parse_gerber_file(str(path))
    return layer, merge_geometries(geoms)


def _drill(tmp_path, text):
    path = tmp_path / "t.drl"
    path.write_text(text)
    return parse_excellon_file(str(path))


def _holes(drills):
    return [(round(x, 4), round(y, 4)) for d in drills for x, y in d.holes]


# ---------------------------------------------------------------- Gerber parser

def test_step_repeat_is_expanded(tmp_path):
    _, m = _gerber(tmp_path, "%ADD10C,1.0*%\n%SRX3Y1I10.0J0*%\nD10*\nX0Y0D03*\n%SR*%\n")
    assert len(m.geoms) == 3
    assert m.bounds == pytest.approx((-0.5, -0.5, 20.5, 0.5))


def test_block_aperture_is_flashed(tmp_path):
    body = "%ADD10C,1.0*%\n%ABD12*%\nD10*\nX0Y0D03*\nX2000000Y0D03*\n%AB*%\nD12*\nX10000000Y0D03*\n"
    _, m = _gerber(tmp_path, body)
    assert m.bounds == pytest.approx((9.5, -0.5, 12.5, 0.5))


def test_region_with_two_contours_is_not_bridged(tmp_path):
    body = ("G36*\nX0Y0D02*\nX1000000Y0D01*\nX1000000Y1000000D01*\nX0Y1000000D01*\nX0Y0D01*\n"
            "X5000000Y0D02*\nX6000000Y0D01*\nX6000000Y1000000D01*\nX5000000Y1000000D01*\nX5000000Y0D01*\nG37*\n")
    _, m = _gerber(tmp_path, body)
    assert len(m.geoms) == 2
    assert m.area == pytest.approx(2.0)


def test_region_inner_contour_is_a_hole(tmp_path):
    body = ("G36*\nX0Y0D02*\nX10000000Y0D01*\nX10000000Y10000000D01*\nX0Y10000000D01*\nX0Y0D01*\n"
            "X2000000Y2000000D02*\nX4000000Y2000000D01*\nX4000000Y4000000D01*\nX2000000Y4000000D01*\n"
            "X2000000Y2000000D01*\nG37*\n")
    _, m = _gerber(tmp_path, body)
    assert m.area == pytest.approx(96.0)


def test_region_overlapping_contours_merge(tmp_path):
    body = ("G36*\nX0Y0D02*\nX10000000Y0D01*\nX10000000Y10000000D01*\nX0Y10000000D01*\nX0Y0D01*\n"
            "X5000000Y5000000D02*\nX15000000Y5000000D01*\nX15000000Y15000000D01*\n"
            "X5000000Y15000000D01*\nX5000000Y5000000D01*\nG37*\n")
    _, m = _gerber(tmp_path, body)
    assert m.area == pytest.approx(175.0)


def test_excellon_routing_moves_update_the_position_incremental(tmp_path):
    _, drills = _drill(tmp_path, "M48\nMETRIC\nICI,ON\nT1C1.0\n%\nT1\nX1.0Y1.0\nG00X4.0Y4.0\n"
                                 "M15\nG01X3.0\nM16\nG05\nX1.0\nM30\n")
    assert drills[0].holes == [(1.0, 1.0), (9.0, 5.0)]


def test_excellon_routing_moves_keep_modal_coordinates_absolute(tmp_path):
    _, drills = _drill(tmp_path, "M48\nMETRIC\nT1C1.0\n%\nG90\nT1\nX1.0Y1.0\nG00X5.0Y5.0\n"
                                 "M15\nG01X8.0Y5.0\nM16\nG05\nX9.0\nM30\n")
    assert drills[0].holes == [(1.0, 1.0), (9.0, 5.0)]


def test_macro_primitive_with_exposure_off_is_subtracted(tmp_path):
    body = "%AMDONUT*\n1,1,2.0,0,0*\n1,0,1.0,0,0*%\n%ADD11DONUT*%\nD11*\nX0Y0D03*\n"
    _, m = _gerber(tmp_path, body)
    assert m.area == pytest.approx(math.pi * (1.0 - 0.25), rel=0.01)
    assert not m.contains(Point(0, 0))


def test_rectangular_aperture_line_sweeps_the_rectangle(tmp_path):
    _, m = _gerber(tmp_path, "%ADD10R,0.2X2.0*%\nD10*\nX0Y0D02*\nX10000000Y0D01*\n")
    assert m.bounds == pytest.approx((-0.1, -1.0, 10.1, 1.0))
    assert m.area == pytest.approx(10.2 * 2.0)


def test_circle_aperture_hole_is_not_flashed_solid(tmp_path):
    _, m = _gerber(tmp_path, "%ADD10C,2.0X1.0*%\nD10*\nX0Y0D03*\n")
    assert not m.contains(Point(0, 0))
    assert m.area == pytest.approx(math.pi * (1.0 - 0.25), rel=0.01)


# ---------------------------------------------------------------- Excellon

def test_route_mode_moves_are_not_holes(tmp_path):
    _, drills = _drill(tmp_path, "M48\nMETRIC\nT1C1.0\n%\nT1\nX1.0Y1.0\nG00X5.0Y5.0\nM15\n"
                                 "G01X8.0Y5.0\nX8.0Y9.0\nM16\nG05\nX2.0Y2.0\nM30\n")
    assert _holes(drills) == [(1.0, 1.0), (2.0, 2.0)]


def test_inline_tool_definition_selects_the_tool(tmp_path):
    _, drills = _drill(tmp_path, "M48\nMETRIC\n%\nT1C0.8\nX1.0Y1.0\nX2.0Y2.0\nM30\n")
    assert _holes(drills) == [(1.0, 1.0), (2.0, 2.0)]
    assert drills[0].diameter == pytest.approx(0.8)


def test_incremental_coordinates_are_accumulated(tmp_path):
    _, drills = _drill(tmp_path, "M48\nMETRIC\nT1C1.0\n%\nG91\nT1\nX1.0Y1.0\nX1.0Y1.0\nX0.5\nM30\n")
    assert _holes(drills) == [(1.0, 1.0), (2.0, 2.0), (2.5, 2.0)]


# ---------------------------------------------------------------- outline origin

def test_work_origin_is_the_outline_centre_line(tmp_path):
    body = ("%ADD10C,0.2*%\nD10*\nX10000000Y10000000D02*\nX110000000Y10000000D01*\n"
            "X110000000Y60000000D01*\nX10000000Y60000000D01*\nX10000000Y10000000D01*\n")
    layer, _ = _gerber(tmp_path, body)
    layer.layer_type = LayerType.OUTLINE
    assert layer.stroke_width == pytest.approx(0.2)
    project = PCBProject()
    project.add_layer(layer)
    assert (project.board_min_x, project.board_min_y) == pytest.approx((10.0, 10.0))
    assert (project.board_width, project.board_height) == pytest.approx((100.0, 50.0))


# ---------------------------------------------------------------- isolation

TIP_SETTINGS = {"tool_diameter": 0.1, "tool_angle": 60, "cut_depth": 0.1}


def _intrusion(gcode, copper, settings):
    """Deepest overlap of any groove with copper, in mm."""
    tan_half = math.tan(math.radians(settings.tool_angle / 2))
    worst = 0.0
    for m in moves(gcode):
        z = min(m["start"][2], m["end"][2])
        if z >= 0 or m["rapid"] or m["start"][:2] == m["end"][:2]:
            continue
        half = settings.tool_diameter / 2 + -z * tan_half
        seg = LineString([m["start"][:2], m["end"][:2]])
        worst = max(worst, half - seg.distance(copper))
    return worst


def test_each_narrow_gap_is_decided_on_its_own():
    """A gap too shallow to cut must not stop the wider ones from being cut."""
    s = ToolpathSettings(**TIP_SETTINGS)
    copper = MultiPolygon([box(0, 0, 5, 1), box(0, 1.12, 5, 2), box(10, 0, 15, 1), box(10, 1.2, 15, 2)])
    g = generate_isolation_gcode(copper, s)
    assert "WARNING: gap 0.120" in g  # no cut possible: reported
    assert "WARNING: gap 0.200" not in g
    paths = cutting_paths(g)
    in_gap = [p for p in paths if any(x > 9 and abs(y - 1.1) < 0.02 for x, y in p)]
    assert in_gap, "the 0.2 mm gap stays uncut"
    # and nothing cut into the neighbour
    assert _intrusion(g, unary_union(list(copper.geoms)), s) < 1e-3


def test_narrow_gap_groove_keeps_out_of_neighbour_copper():
    s = ToolpathSettings(**TIP_SETTINGS)
    boxes = [box(10, 0, 15, 1), box(10, 1.16, 15, 2), box(0, 0, 5, 1), box(0, 1.05, 5, 2)]
    g = generate_isolation_gcode(MultiPolygon(boxes), s)
    assert _intrusion(g, unary_union(boxes), s) < 1e-3
    assert any(x > 9 and abs(y - 1.08) < 0.01 for p in cutting_paths(g) for x, y in p)


# ---------------------------------------------------------------- direction (outline)

def test_mirrored_outline_is_still_climb_milled():
    from gcode_reader import signed_area
    s = ToolpathSettings(tool_diameter=2.0, num_tabs=0, outline_step_down=2.0)
    for kwargs in ({}, {"mirror": "X", "board_size": (100.0, 50.0)}):
        paths = cutting_paths(generate_outline_gcode(box(0, 0, 100, 50), s, board_thickness=1.6, **kwargs))
        assert paths and all(signed_area(p) < 0 for p in paths), kwargs  # board on the right


# ---------------------------------------------------------------- alignment fit

def test_alignment_translation_belongs_to_the_normalised_rotation():
    expected = [(55.0, 35.0), (105.0, 75.0)]
    measured = [(x * 1.003 + 0.5, y * 1.003 - 0.3) for x, y in expected]
    t = compute_alignment_transform(expected, measured)
    cx = sum(p[0] for p in expected) / 2
    cy = sum(p[1] for p in expected) / 2
    mx = sum(p[0] for p in measured) / 2
    my = sum(p[1] for p in measured) / 2
    # centroid maps onto centroid
    assert t.cos_theta * cx - t.sin_theta * cy + t.tx == pytest.approx(mx, abs=1e-9)
    assert t.sin_theta * cx + t.cos_theta * cy + t.ty == pytest.approx(my, abs=1e-9)


# ---------------------------------------------------------------- mask exposure

def test_mask_exposure_starts_at_the_bottom_edge_of_the_keep_area():
    s = ToolpathSettings(uv_spot_size=1.0, uv_line_overlap=0.0)
    g = generate_mask_expose_gcode(None, (0, 0, 10, 10), s)
    first = next(m for m in moves(g) if "Y" in m["line"])
    assert first["end"][1] == pytest.approx(0.5)  # eroded edge, not 0.5 + spot / 2


# ---------------------------------------------------------------- autolevel arcs

def test_arc_updates_the_tracked_position():
    hm = HeightMap()
    hm.generate_grid(0, 0, 40, 10, 2, 2)
    for i in range(len(hm.points)):
        hm.set_probe_result(i, 0.0)
    hm.build_interpolator()
    gcode = "G0 X0 Y0\nG1 Z-0.1 F100\nG2 X10 Y0 I5 J0 F300\nG1 X30"
    out = apply_autolevel(gcode, hm).splitlines()
    tail = [l for l in out if l.startswith("G1 X")][-3:]
    xs = [float(l.split("X")[1].split()[0]) for l in tail]
    assert min(xs) > 10.0, out  # the line after the arc starts at the arc end, not at the arc start
    # the arc is followed on its own path: a half circle through (5, 5): G2 runs clockwise
    pts = [(float(l.split("X")[1].split()[0]), float(l.split("Y")[1].split()[0]))
           for l in out if l.startswith("G1 X") and "Z" in l][:20]
    assert any(abs(x - 5) < 0.6 and y > 4.4 for x, y in pts)
