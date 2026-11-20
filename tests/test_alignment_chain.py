"""Double-sided alignment: hole positions, work coordinates, G-code chain."""
import logging
import queue

import pytest
from flask import Flask
from flask_babel import Babel
from shapely.geometry import box

from octoprint_grblpcb import GrblPcbPlugin
from octoprint_grblpcb.pcb_project import DrillInfo, MillingOperation, PCBProject
from octoprint_grblpcb.toolpath import back_side_alignment_holes


def test_back_side_holes_are_mirrored_work_coordinates():
    holes = [(44.5, -249.0, 2.5), (44.5, -262.0, 2.5), (100.0, -140.0, 1.0)]
    selected = back_side_alignment_holes(holes, (40.0, -270.0), (113.0, 253.0), "X")
    # farthest pair: (44.5,-262) and (100,-140); front work coords
    # (4.5, 8.0) and (60.0, 130.0); mirrored in X across width 113
    assert {(h["expected_x"], h["expected_y"]) for h in selected} == {(108.5, 8.0), (53.0, 130.0)}


def test_back_side_holes_mirror_y():
    selected = back_side_alignment_holes([(0.0, 0.0, 1.0), (10.0, 20.0, 1.0)], (0.0, 0.0), (10.0, 20.0), "Y")
    assert {(h["expected_x"], h["expected_y"]) for h in selected} == {(0.0, 20.0), (10.0, 0.0)}


class _Settings:
    def get_float(self, path):
        return 1.0

    def get_int(self, path):
        return 3


def _plugin():
    plugin = GrblPcbPlugin()
    plugin._settings = _Settings()
    plugin._save_project = lambda: None
    plugin._logger = logging.getLogger("test")
    project = PCBProject()
    project.board_width = 50.0
    project.board_height = 30.0
    project.operations = [
        MillingOperation(name="Iso", operation_type="isolation", layer_type="bottom_copper",
                         gcode="G1 X10 Y0 Z-0.1", mirrored=True),
    ]
    project.alignment_transform = {"theta_deg": 0.0, "theta_rad": 0.0, "tx": 1.0, "ty": 2.0,
                                   "cos_theta": 1.0, "sin_theta": 0.0, "rms_error": 0.0}
    plugin._project = project
    return plugin


@pytest.fixture
def app():
    app = Flask(__name__)
    Babel(app)
    return app


def test_alignment_applied_twice_does_not_stack(app):
    plugin = _plugin()
    with app.app_context():
        plugin._handle_apply_alignment({"operation_index": 0})
        first = plugin._project.operations[0].gcode_aligned
        plugin._handle_apply_alignment({"operation_index": 0})
    assert plugin._project.operations[0].gcode_aligned == first
    assert "X11" in first and "Y2" in first


def test_autolevel_builds_on_aligned_gcode_and_send_prefers_it(app):
    from octoprint_grblpcb.autolevel import HeightMap

    plugin = _plugin()
    hm = HeightMap()
    hm.generate_grid(0, 0, 50, 30, grid_x=2, grid_y=2, margin=0)
    for i in range(len(hm.points)):
        hm.set_probe_result(i, 0.0)
    hm.build_interpolator()
    plugin._height_map = hm
    op = plugin._project.operations[0]
    with app.app_context():
        plugin._handle_apply_alignment({"operation_index": 0})
        assert op.gcode_to_send == op.gcode_aligned
        plugin._handle_apply_autolevel({"operation_index": 0, "max_segment": 100})
    assert "X11" in op.gcode_leveled  # leveled from the aligned positions
    assert op.gcode_to_send == op.gcode_leveled


def test_new_alignment_discards_derived_gcode(app):
    plugin = _plugin()
    op = plugin._project.operations[0]
    op.gcode_aligned, op.gcode_leveled = "aligned", "leveled"
    with app.app_context():
        plugin._handle_clear_alignment({})
    assert op.gcode_aligned == "" and op.gcode_leveled == ""


def test_auto_alignment_holes_uses_project_drills(app):
    plugin = _plugin()
    p = plugin._project
    p.board_min_x, p.board_min_y = 40.0, -270.0
    p.board_width, p.board_height = 113.0, 253.0
    p.flip_axis = "X"
    p.drills = [DrillInfo(tool_number=1, diameter=2.5, holes=[(44.5, -249.0), (100.0, -140.0)], count=2)]
    with app.app_context():
        body = plugin._handle_auto_alignment_holes({}).get_json()
    assert {(h["expected_x"], h["expected_y"]) for h in body["alignment_holes"]} == {(108.5, 21.0), (53.0, 130.0)}


class _Comm:
    def __init__(self, *responses):
        self._response_queue = queue.Queue()
        for r in responses:
            self._response_queue.put(r)


class _Connector:
    def __init__(self, comm):
        self._comm = comm
        self._work_offset = {"x": 100.0, "y": 50.0, "z": -1.0}
        self.sent = []

    def commands(self, *commands, **kwargs):
        self.sent.extend(commands)
        for _ in commands:
            if _.startswith("G21"):
                self._comm._response_queue.put(("ok",))


def test_probe_axis_uses_g38_3_and_returns_work_coordinates():
    plugin = _plugin()
    comm = _Comm()
    connector = _Connector(comm)
    # after G21/G90 oks arrive, the probe answers in machine coordinates
    original = connector.commands

    def commands(*c, **kw):
        original(*c, **kw)
        if any(x.startswith("G38") for x in c):
            comm._response_queue.put(("probe", {"x": 112.5, "y": 60.0, "z": -1.5, "success": True}))
            comm._response_queue.put(("ok",))

    connector.commands = commands
    assert plugin._probe_axis(comm, connector, "X", 14.0, 25.0) == pytest.approx(12.5)
    assert any(c.startswith("G38.3 X14.0000") for c in connector.sent)


def test_probe_axis_without_wall_returns_none():
    plugin = _plugin()
    comm = _Comm()
    connector = _Connector(comm)
    original = connector.commands

    def commands(*c, **kw):
        original(*c, **kw)
        if any(x.startswith("G38") for x in c):
            comm._response_queue.put(("probe", {"x": 0.0, "y": 0.0, "z": 0.0, "success": False}))
            comm._response_queue.put(("ok",))

    connector.commands = commands
    assert plugin._probe_axis(comm, connector, "X", 14.0, 25.0) is None


def test_mirror_regenerates_from_geometry(app):
    from octoprint_grblpcb.pcb_project import LayerType

    plugin = _plugin()
    plugin._settings = type("S", (), {
        "get_float": lambda self, path: {"tool_diameter": 0.1, "tool_angle": 60, "cut_depth": 0.1}.get(path[0], 1.0),
        "get_int": lambda self, path: 1,
        "get_boolean": lambda self, path: False,
        "get": lambda self, path: "G54",
    })()
    p = plugin._project
    p.operations = []
    p.board_min_x, p.board_min_y, p.board_width, p.board_height = 0.0, 0.0, 50.0, 30.0
    p.flip_axis = "X"
    p.drills = [DrillInfo(tool_number=1, diameter=0.8, holes=[(10.0, 10.0), (40.0, 20.0)], count=2)]
    with app.app_context():
        plugin._handle_generate_drill({})
        body = plugin._handle_mirror_gcode({"operation_index": 0}).get_json()
    assert len(p.operations) == 2
    front, back = p.operations
    assert not front.mirrored and back.mirrored
    assert body["operation"]["mirrored"] is True
    assert "Back side: mirrored in X" in back.gcode
    # mirroring twice replaces the back-side operation instead of adding one
    with app.app_context():
        plugin._handle_mirror_gcode({"operation_index": 0})
    assert len(p.operations) == 2

    # Top copper belongs to the front: it is never mirrored
    plugin._geometries = {LayerType.TOP_COPPER: [box(9, 9, 11, 11)]}
    with app.app_context():
        plugin._handle_generate_isolation({"layer_type": "top_copper"})
        top_index = next(i for i, op in enumerate(p.operations) if op.layer_type == "top_copper")
        assert p.operations[top_index].to_dict()["can_mirror"] is False
        _response, status = plugin._handle_mirror_gcode({"operation_index": top_index})
    assert status == 400


def test_old_operation_without_generation_cannot_be_mirrored(app):
    plugin = _plugin()
    plugin._project.operations[0].mirrored = False
    plugin._project.operations[0].generation = None
    with app.app_context():
        _response, status = plugin._handle_mirror_gcode({"operation_index": 0})
    assert status == 400
