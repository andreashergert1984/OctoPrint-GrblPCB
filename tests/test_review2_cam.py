"""Regressions from the second CAM review: rotation, autolevel, parsers, mirroring."""
import logging
import pathlib

import pytest
from flask import Flask
from flask_babel import Babel
from shapely.geometry import Point

from octoprint_grblpcb import GrblPcbPlugin
from octoprint_grblpcb.autolevel import HeightMap, apply_autolevel
from octoprint_grblpcb.gerber_parser import (
    _TOOL_SELECT,
    merge_geometries,
    parse_gerber_file,
    thermal_shape,
)
from octoprint_grblpcb.pcb_project import LayerType, MillingOperation
from octoprint_grblpcb.toolpath import ToolpathSettings, generate_isolation_gcode

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "two_layer"


class _Settings:
    def __init__(self, **values):
        self.values = {**GrblPcbPlugin().get_settings_defaults(), **values}

    def get(self, path):
        return self.values.get(path[0])

    def get_int(self, path):
        v = self.values.get(path[0])
        return None if v is None else int(v)

    def get_boolean(self, path):
        return bool(self.values.get(path[0]))

    def get_float(self, path):
        v = self.values.get(path[0])
        return None if v is None else float(v)


@pytest.fixture
def app():
    app = Flask(__name__)
    Babel(app)
    return app


def _plugin(tmp_path, **settings):
    plugin = GrblPcbPlugin()
    plugin._settings = _Settings(**settings)
    plugin._logger = logging.getLogger("test")
    plugin._upload_dir = str(tmp_path)
    plugin._save_project = lambda: None
    plugin._project = plugin._make_project(name="t")
    return plugin


def _two_layer(plugin):
    for name, lt in [("TwoLayer-F_Cu.gbr", LayerType.TOP_COPPER), ("TwoLayer-B_Cu.gbr", LayerType.BOTTOM_COPPER),
                     ("TwoLayer-Edge_Cuts.gbr", LayerType.OUTLINE), ("TwoLayer.drl", LayerType.DRILL)]:
        plugin._parse_and_add_layer(str(FIXTURE / name), lt)


def _with_alignment(plugin):
    p = plugin._project
    p.alignment_holes = [{"expected_x": 1.0, "expected_y": 1.0, "diameter": 1.0, "probed": True,
                          "measured_x": 1.0, "measured_y": 1.0}]
    p.alignment_transform = {"tx": 1.0}
    p.alignment_probed_at = 123.0
    p.operations = [MillingOperation(name="m", operation_type="drill", layer_type="drill", gcode="G0",
                                     gcode_aligned="G1", mirrored=True)]


def _assert_alignment_gone(p):
    assert p.alignment_holes == [] and p.alignment_transform is None and p.alignment_probed_at is None
    assert all(not op.gcode_aligned for op in p.operations)


# --- CAM 1 + 3: rotation ------------------------------------------------------

def test_four_rotations_restore_layer_bounds_and_drill_bounds(app, tmp_path):
    plugin = _plugin(tmp_path)
    _two_layer(plugin)
    p = plugin._project
    start = (p.board_width, p.board_height)
    drill_start = next((l.min_x, l.min_y, l.max_x, l.max_y) for t, l in p.layers.items() if t == LayerType.DRILL)
    with app.app_context():
        plugin._handle_rotate_project({"angle": 90})
        drill_90 = p.layers[LayerType.DRILL]
        assert drill_90.width == pytest.approx(drill_start[3] - drill_start[1])  # width and height swapped
        for _ in range(3):
            plugin._handle_rotate_project({"angle": 90})
    assert p.rotation == 0
    assert (p.board_width, p.board_height) == pytest.approx(start)
    outline = merge_geometries(plugin._geometries[LayerType.OUTLINE]).bounds
    layer = p.layers[LayerType.OUTLINE]
    assert (layer.min_x, layer.min_y, layer.max_x, layer.max_y) == pytest.approx(outline)
    d = p.layers[LayerType.DRILL]
    assert (d.min_x, d.min_y, d.max_x, d.max_y) == pytest.approx(drill_start)


def test_rotation_resets_alignment(app, tmp_path):
    plugin = _plugin(tmp_path)
    _two_layer(plugin)
    _with_alignment(plugin)
    with app.app_context():
        plugin._handle_rotate_project({"angle": 90})
    _assert_alignment_gone(plugin._project)


def test_drill_replace_and_remove_reset_alignment(app, tmp_path):
    plugin = _plugin(tmp_path)
    _two_layer(plugin)
    _with_alignment(plugin)
    plugin._parse_and_add_layer(str(FIXTURE / "TwoLayer.drl"), LayerType.DRILL)
    _assert_alignment_gone(plugin._project)
    _with_alignment(plugin)
    with app.app_context():
        plugin._handle_remove_layer({"layer_type": "drill"})
    _assert_alignment_gone(plugin._project)


# --- CAM 2: autolevel with unknown start ------------------------------------

def _level(gcode):
    hm = HeightMap()
    hm.generate_grid(0, 0, 10, 10, 2, 2, margin=0)
    for pt in hm.points:
        pt.z = 0.3
        pt.probed = True
    hm.completed = True
    hm.build_interpolator()
    return apply_autolevel(gcode, hm)


def test_z_only_line_never_gains_xy_words():
    out = _level("G1 Z5 F120 ; safe Z\nG0 Z2\nG0 X1 Y1\nG1 Z-0.1 F50\nG1 Z2")
    lines = out.splitlines()
    assert lines[0] == "G1 Z5 F120 ; safe Z"  # untouched, XY unknown
    assert lines[1] == "G0 Z2"
    z_only = [ln for ln in lines[3:] if "X" not in ln]
    assert z_only and all("Y" not in ln for ln in z_only)
    assert "Z-0.1" not in out  # corrected by the +0.3 surface offset
    assert "Z0.2" in out


def test_first_xy_move_is_not_split_from_an_invented_origin():
    out = _level("G1 X8 Y8 Z-0.1 F50")
    assert len(out.splitlines()) == 1


# --- CAM 4: Polygon aperture --------------------------------------------------

def test_polygon_aperture_is_a_regular_ngon(tmp_path):
    f = tmp_path / "p.gbr"
    f.write_text("%FSLAX46Y46*%\n%MOMM*%\n%ADD10P,3.0X5X0*%\n%ADD11P,3.0X6X30*%\nG01*\n%LPD*%\n"
                 "D10*\nX5000000Y5000000D03*\nD11*\nX15000000Y5000000D03*\nM02*\n")
    layer, geoms = parse_gerber_file(str(f))
    pent, hexa = geoms
    assert len(pent.exterior.coords) - 1 == 5
    assert len(hexa.exterior.coords) - 1 == 6
    # circumscribed circle radius 1.5; the circle itself would be bigger in area
    assert pent.area == pytest.approx(5 * 0.5 * 1.5**2 * 0.9510565, rel=1e-3)
    assert not pent.equals(Point(5, 5).buffer(1.5))
    # first vertex of the hexagon at 30 degrees
    x, y = hexa.exterior.coords[0]
    assert (x - 15, y - 5) == pytest.approx((1.5 * 0.8660254, 0.75), abs=1e-3)
    assert layer.unsupported_shapes == 0


# --- CAM 5: thermal macro -----------------------------------------------------

def test_thermal_primitive_is_a_ring_with_gaps(tmp_path):
    f = tmp_path / "t.gbr"
    f.write_text("%FSLAX46Y46*%\n%MOMM*%\n%AMTHERM*\n7,0,0,2.0,1.0,0.3,0*%\n%ADD12THERM*%\nG01*\n%LPD*%\n"
                 "D12*\nX5000000Y5000000D03*\nM02*\n")
    layer, geoms = parse_gerber_file(str(f))
    assert layer.unsupported_shapes == 0
    shape = merge_geometries(geoms)
    assert shape.bounds == pytest.approx((4.0, 4.0, 6.0, 6.0), abs=0.02)
    assert not shape.contains(Point(5, 5))  # hollow centre
    assert not shape.intersects(Point(5.75, 5.0))  # inside the gap
    assert shape.intersects(Point(5.6, 5.6))  # ring quadrant
    assert shape.area == pytest.approx(thermal_shape(0, 0, 2.0, 1.0, 0.3, 0).area, rel=1e-6)


def test_macro_without_shape_is_reported_not_a_dot(tmp_path):
    f = tmp_path / "m.gbr"
    f.write_text("%FSLAX46Y46*%\n%MOMM*%\n%AMEMPTYM*\n1,0,1.0,0,0*%\n%ADD12EMPTYM*%\nG01*\n%LPD*%\n"
                 "D12*\nX5000000Y5000000D03*\nM02*\n")
    layer, geoms = parse_gerber_file(str(f))
    assert layer.unsupported_shapes == 1
    assert geoms == []
    assert layer.to_dict()["unsupported_shapes"] == 1


# --- CAM 6: Excellon tool select ---------------------------------------------

def test_tool_select_accepts_feed_speed_suffixes():
    for line in ("T1", "T01F00S00", "T2S12000", "T3F200S100"):
        assert _TOOL_SELECT.match(line), line
    assert not _TOOL_SELECT.match("T1C0.8")


def test_excellon_tool_select_with_suffix_selects_tool(tmp_path):
    from octoprint_grblpcb.gerber_parser import parse_excellon_file
    f = tmp_path / "a.drl"
    f.write_text("M48\nMETRIC,TZ\nT1C0.8\nT2C1.0\n%\nT1F00S00\nX1.0Y1.0\nT2F00S00\nX5.0Y5.0\nM30\n")
    _, drills = parse_excellon_file(str(f))
    by_tool = {d.tool_number: d for d in drills}
    assert by_tool[1].holes == [(1.0, 1.0)] and by_tool[2].holes == [(5.0, 5.0)]


# --- CAM 7: board side / flip axis -------------------------------------------

def test_board_side_rejects_unknown_flip_axis(app, tmp_path):
    plugin = _plugin(tmp_path)
    with app.app_context():
        resp = plugin._handle_set_board_side({"side": "double", "flip_axis": "Z"})
    assert resp[1] == 400
    assert plugin._project.flip_axis == "X"


def test_changing_flip_axis_drops_mirrored_operations_and_alignment(app, tmp_path):
    plugin = _plugin(tmp_path)
    _with_alignment(plugin)
    plugin._project.operations.append(MillingOperation(name="front", operation_type="isolation",
                                                       layer_type="top_copper", gcode="G0"))
    with app.app_context():
        plugin._handle_set_board_side({"side": "double", "flip_axis": "y"})
    p = plugin._project
    assert p.flip_axis == "Y"
    assert [op.name for op in p.operations] == ["front"]
    assert p.alignment_holes == [] and p.alignment_transform is None


def test_same_flip_axis_keeps_operations(app, tmp_path):
    plugin = _plugin(tmp_path)
    _with_alignment(plugin)
    with app.app_context():
        plugin._handle_set_board_side({"side": "double"})  # axis not sent: unchanged
    assert len(plugin._project.operations) == 1 and plugin._project.alignment_holes


def test_flip_axis_setting_is_the_default_for_new_projects(app, tmp_path):
    plugin = _plugin(tmp_path, flip_axis="Y")
    with app.app_context():
        plugin._handle_new_project({"name": "x"})
    assert plugin._project.flip_axis == "Y"
    assert _plugin(tmp_path, flip_axis="junk")._project.flip_axis == "X"


# --- CAM 8: bottom-layer gap warning coordinates ------------------------------

def test_bottom_gap_warning_uses_mirrored_coordinates(app, tmp_path):
    from shapely.geometry import box
    plugin = _plugin(tmp_path, tool_diameter=0.5)
    # two pads 0.2 mm apart near the left edge of a 40 mm wide board
    plugin._geometries[LayerType.BOTTOM_COPPER] = [box(2, 2, 3, 3), box(3.2, 2, 4.2, 3)]
    p = plugin._project
    p.board_width, p.board_height = 40.0, 20.0
    p.board_min_x = p.board_min_y = 0.0
    plugin._settings.values.update(isolation_clear_gaps=False, isolation_max_width=0)
    with app.app_context():
        resp = plugin._handle_generate_isolation({"layer_type": "bottom_copper"})
        body = resp.get_json()
    gap = next(w for w in body["warnings"] if w["code"] == "unisolatable_gaps")
    assert gap["x"] == pytest.approx(40.0 - 3.1, abs=0.1)  # mirrored in X


# --- suspected: alignment hole choice, probe depth, round joins ---------------

def test_auto_alignment_prefers_holes_the_probe_can_enter(app, tmp_path):
    from octoprint_grblpcb.pcb_project import DrillInfo
    plugin = _plugin(tmp_path)
    p = plugin._project
    p.board_width, p.board_height = 100.0, 100.0
    p.drills = [
        DrillInfo(tool_number=1, diameter=0.4, holes=[(0.0, 0.0), (100.0, 100.0)], count=2),
        DrillInfo(tool_number=2, diameter=1.5, holes=[(10.0, 10.0), (60.0, 20.0)], count=2),
    ]
    with app.app_context():
        body = plugin._handle_auto_alignment_holes({}).get_json()
    assert {h["diameter"] for h in body["alignment_holes"]} == {1.5}
    assert "warning" not in body
    p.drills = [DrillInfo(tool_number=1, diameter=0.4, holes=[(0.0, 0.0), (100.0, 100.0)], count=2)]
    with app.app_context():
        body = plugin._handle_auto_alignment_holes({}).get_json()
    assert body["warning"]  # fallback is announced


def test_probe_sequence_depth_is_always_negative():
    hm = HeightMap()
    hm.generate_grid(0, 0, 10, 10, 2, 2, margin=0)
    from octoprint_grblpcb.autolevel import generate_probe_gcode
    code = generate_probe_gcode(hm, probe_depth=5.0)
    assert "G38.2 Z-5.0" in code and "G38.2 Z5" not in code


def test_isolation_offsets_use_round_joins():
    """Acute corner: successive passes stay one step apart (no mitre spikes/slivers)."""
    from shapely.geometry import Polygon
    tri = Polygon([(0, 0), (10, 0), (0.5, 3)])
    s = ToolpathSettings(tool_diameter=0.2, tool_angle=30, cut_depth=0.1, passes=3, pass_overlap=50)
    code = generate_isolation_gcode(tri, s)
    import re
    xs = [float(v) for v in re.findall(r"X(-?[\d.]+)", code)]
    # a mitre join would reach far beyond offset 3 * step around the acute tip
    assert min(xs) > -(s.effective_width * 3 + 1.0)


def test_outline_inset_uses_the_stroke_of_the_board_ring_not_the_widest_stroke(tmp_path):
    f = tmp_path / "o.gbr"
    f.write_text("%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.2*%\n%ADD11C,1.0*%\nG01*\n%LPD*%\nD10*\n"
                 "X10000000Y10000000D02*\nX110000000Y10000000D01*\nX110000000Y60000000D01*\n"
                 "X10000000Y60000000D01*\nX10000000Y10000000D01*\n"
                 "D11*\nX40000000Y20000000D02*\nX45000000Y20000000D01*\nM02*\n")  # wide stray stroke
    layer, _ = parse_gerber_file(str(f))
    assert layer.stroke_width == pytest.approx(0.2)
