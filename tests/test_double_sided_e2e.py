"""
Double-sided board end to end on the two-layer fixture: front and back
toolpaths must register with the drilled holes after flipping the board.
"""
import pathlib

import pytest
from shapely.geometry import Point, Polygon

from gcode_reader import cutting_paths, signed_area
from octoprint_grblpcb.gerber_parser import merge_geometries, parse_excellon_file, parse_gerber_file
from octoprint_grblpcb.toolpath import (
    ToolpathSettings,
    back_side_alignment_holes,
    generate_drill_gcode,
    generate_isolation_gcode,
    generate_outline_gcode,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "two_layer"
SETTINGS = dict(tool_diameter=0.1, tool_angle=60, cut_depth=0.1)


@pytest.fixture(scope="module")
def board():
    top = merge_geometries(parse_gerber_file(str(FIXTURE / "TwoLayer-F_Cu.gbr"))[1])
    bottom = merge_geometries(parse_gerber_file(str(FIXTURE / "TwoLayer-B_Cu.gbr"))[1])
    edge_layer, edge = parse_gerber_file(str(FIXTURE / "TwoLayer-Edge_Cuts.gbr"))
    _layer, drills = parse_excellon_file(str(FIXTURE / "TwoLayer.drl"))
    # Board origin and size as the plugin takes them: bounds of the outline layer
    minx, miny, maxx, maxy = merge_geometries(edge).bounds
    return {
        "top": top, "bottom": bottom, "edge": merge_geometries(edge), "drills": drills,
        "origin": (minx, miny), "size": (maxx - minx, maxy - miny),
    }


def _rings_around(paths, point, max_size=3.0):
    """Closed cutting rings that enclose point and are pad-sized."""
    found = []
    for p in paths:
        if len(p) < 4 or p[0] != p[-1]:
            continue
        poly = Polygon(p)
        minx, miny, maxx, maxy = poly.bounds
        if poly.contains(Point(point)) and maxx - minx < max_size and maxy - miny < max_size:
            found.append(poly)
    return found


def _component_holes(board):
    return [(x, y) for d in board["drills"] if d.diameter < 2 for x, y in d.holes]


def test_front_isolation_rings_enclose_the_holes(board):
    ox, oy = board["origin"]
    g = generate_isolation_gcode(board["top"], ToolpathSettings(**SETTINGS), origin_x=ox, origin_y=oy)
    paths = cutting_paths(g)
    for x, y in _component_holes(board):
        hole = (x - ox, y - oy)
        if Point(x, y).within(board["top"].buffer(0.01)):
            # holes on traces are inside larger rings; pads alone get a pad ring
            assert any(Polygon(p).contains(Point(hole)) for p in paths if p[0] == p[-1]), hole


def test_back_side_registers_with_the_drilled_holes(board):
    """After flipping, every hole drilled from the front sits inside its bottom pad ring."""
    ox, oy = board["origin"]
    w, h = board["size"]
    front_drill = generate_drill_gcode(board["drills"], ToolpathSettings(cut_depth=1.6), origin_x=ox, origin_y=oy)
    back = generate_isolation_gcode(board["bottom"], ToolpathSettings(**SETTINGS), origin_x=ox, origin_y=oy,
                                    mirror="X", board_size=(w, h))
    paths = cutting_paths(back)
    for x, y in _component_holes(board):
        fx, fy = x - ox, y - oy
        assert f"G0 X{fx:.4f} Y{fy:.4f}" in front_drill
        flipped = (w - fx, fy)  # where the drilled hole is after flipping around Y
        assert any(Polygon(p).contains(Point(flipped)) for p in paths if p[0] == p[-1]), flipped


def test_bottom_only_pad_is_milled_mirrored(board):
    ox, oy = board["origin"]
    w, h = board["size"]
    back = generate_isolation_gcode(board["bottom"], ToolpathSettings(**SETTINGS), origin_x=ox, origin_y=oy,
                                    mirror="X", board_size=(w, h))
    smd = (w - (50 - ox), 20 - oy)
    assert _rings_around(cutting_paths(back), smd)
    # and nothing is milled at its unmirrored place
    assert not _rings_around(cutting_paths(back), (50 - ox, 20 - oy))


def test_both_sides_are_climb_milled(board):
    ox, oy = board["origin"]
    w, h = board["size"]
    for copper, mirror in ((board["top"], None), (board["bottom"], "X")):
        g = generate_isolation_gcode(copper, ToolpathSettings(**SETTINGS), origin_x=ox, origin_y=oy,
                                     mirror=mirror, board_size=(w, h))
        outer = [p for p in cutting_paths(g) if p[0] == p[-1] and signed_area(p) != 0]
        # every copper island is surrounded counter-clockwise
        assert sum(1 for p in outer if signed_area(p) > 0) >= len(copper.geoms)


def test_alignment_holes_are_the_mirrored_corner_holes(board):
    ox, oy = board["origin"]
    w, h = board["size"]
    holes = [(x, y, d.diameter) for d in board["drills"] for x, y in d.holes]
    picked = {(round(p["expected_x"], 2), round(p["expected_y"], 2)) for p in back_side_alignment_holes(holes, (ox, oy), (w, h), "X")}
    corners = {(round(w - (x - ox), 2), round(y - oy, 2)) for x, y in [(4, 4), (56, 36), (56, 4), (4, 36)]}
    assert len(picked) == 2 and picked <= corners


def test_outline_on_the_centre_line(board):
    ox, oy = board["origin"]
    g = generate_outline_gcode(board["edge"], ToolpathSettings(tool_diameter=2.0, num_tabs=4), board_thickness=1.6,
                               origin_x=ox, origin_y=oy)
    outer = cutting_paths(g)[-1]
    xs = [x for x, _ in outer]
    # centre line 0..60 in design coordinates, tool 1 mm outside
    assert (min(xs) + ox, max(xs) + ox) == pytest.approx((-1.0, 61.0), abs=0.02)
