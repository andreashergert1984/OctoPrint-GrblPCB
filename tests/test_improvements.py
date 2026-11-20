"""Ring ordering, alignment transform, Gerber polarity, Excellon slots, permissions, project save."""
import json
import math
import os
import random

import pytest
from flask import Flask
from flask_babel import Babel
from shapely.geometry import Point, box

from octoprint_grblpcb import GrblPcbPlugin, gerber_parser
from octoprint_grblpcb.gerber_parser import parse_excellon_file, parse_gerber_file
from octoprint_grblpcb.pcb_project import PCBProject
from octoprint_grblpcb.toolpath import (
    AlignmentTransform,
    _order_rings,
    apply_alignment_transform,
)


def _reference_order(rings, start=(0.0, 0.0)):
    """The plain nearest-vertex search the fast version must reproduce."""
    remaining = [r[:-1] if r[0] == r[-1] else list(r) for r in rings]
    ordered = []
    pos = start
    while remaining:
        best = None
        for ri, ring in enumerate(remaining):
            for vi, (x, y) in enumerate(ring):
                d = (x - pos[0]) ** 2 + (y - pos[1]) ** 2
                if best is None or d < best[0]:
                    best = (d, ri, vi)
        _, ri, vi = best
        ring = remaining.pop(ri)
        ring = ring[vi:] + ring[:vi]
        ring.append(ring[0])
        ordered.append(ring)
        pos = ring[0]
    return ordered


def test_order_rings_matches_the_plain_search():
    rng = random.Random(7)
    rings = []
    for _ in range(60):
        cx, cy, n = rng.uniform(0, 100), rng.uniform(0, 80), rng.randint(3, 12)
        pts = [(cx + math.cos(2 * math.pi * i / n), cy + math.sin(2 * math.pi * i / n)) for i in range(n)]
        rings.append(pts + [pts[0]])
    assert _order_rings(rings, (10.0, 5.0)) == _reference_order(rings, (10.0, 5.0))
    assert _order_rings([]) == []


def _rot(deg, tx=0.0, ty=0.0):
    r = math.radians(deg)
    return AlignmentTransform(cos_theta=math.cos(r), sin_theta=math.sin(r), tx=tx, ty=ty)


def test_alignment_transform_handles_single_axis_moves():
    # 90 degrees: a move along X becomes a move along Y
    out = apply_alignment_transform("G0 X0 Y0\nG1 X10 F100\nG1 Y5", _rot(90)).splitlines()
    assert out[1] == "G1 X-0.0000 Y10.0000 F100" or out[1] == "G1 X0.0000 Y10.0000 F100"
    assert out[2] == "G1 X-5.0000 Y10.0000"


def test_alignment_transform_leaves_comments_and_offsets_alone():
    gcode = "; at X1.0 Y2.0\nG0 X1 Y2 ; back to X0 Y0\nG10 L20 P0 X0 Y0\nT1 M6 ; drill"
    out = apply_alignment_transform(gcode, _rot(0, tx=1.0, ty=1.0)).splitlines()
    assert out[0] == "; at X1.0 Y2.0"
    assert out[1] == "G0 X2.0000 Y3.0000 ; back to X0 Y0"
    assert out[2] == "G10 L20 P0 X0 Y0"
    assert out[3] == "T1 M6 ; drill"


@pytest.mark.parametrize("line", ["G2 X1 Y1 I1 J0", "G91"])
def test_alignment_transform_refuses_what_it_cannot_transform(line):
    with pytest.raises(ValueError):
        apply_alignment_transform(f"G0 X0 Y0\n{line}", _rot(1))


class _Transform:
    def __init__(self, polarity):
        self.polarity = type("P", (), {"name": polarity})()


class _Cmd:
    def __init__(self, geom, polarity):
        self.geom = geom
        self.transform = _Transform(polarity)


def test_clear_polarity_erases_earlier_copper(monkeypatch):
    monkeypatch.setattr(gerber_parser, "_command_to_shapely", lambda cmd: cmd.geom)
    parsed = type("Parsed", (), {})()
    parsed._command_buffer = [
        _Cmd(box(0, 0, 10, 10), "Dark"),
        _Cmd(Point(5, 5).buffer(2), "Clear"),
        _Cmd(Point(5, 5).buffer(0.5), "Dark"),  # pad drawn after the clearance stays
    ]
    merged = gerber_parser.merge_geometries(gerber_parser._extract_geometries(parsed))
    assert merged.area == pytest.approx(100 - Point(5, 5).buffer(2).area + Point(5, 5).buffer(0.5).area)
    assert not merged.contains(Point(6.5, 5))
    assert merged.contains(Point(5, 5))


def test_clear_polarity_in_a_real_gerber(tmp_path):
    gerber = tmp_path / "clear.gbr"
    gerber.write_text(
        "%FSLAX46Y46*%\n%MOMM*%\n%LPD*%\n%ADD10C,4.000000*%\n%ADD11C,2.000000*%\n"
        "D10*\nX0Y0D03*\n%LPC*%\nD11*\nX0Y0D03*\nM02*\n"
    )
    _, geometries = parse_gerber_file(str(gerber))
    merged = gerber_parser.merge_geometries(geometries)
    assert merged.area == pytest.approx(math.pi * (4 - 1), rel=0.01)  # ring 4 mm outside, 2 mm hole


def test_excellon_reports_routed_paths(tmp_path):
    drill = tmp_path / "slots.drl"
    drill.write_text(
        "M48\nMETRIC\nT1C0.800\nT2C1.000\n%\nT1\nX1.0Y1.0\nT2\nX5.0Y5.0\n"
        "G00X10.0Y10.0\nM15\nG01X12.0Y10.0\nM16\nM30\n"
    )
    layer, drills = parse_excellon_file(str(drill))
    # The routed path (M15 ... M16) is not supported and adds no holes
    assert layer.unsupported_slots == 1
    assert layer.to_dict()["unsupported_slots"] == 1
    holes = {d.tool_number: d.holes for d in drills}
    assert holes[1] == [(1.0, 1.0)]
    assert holes[2] == [(5.0, 5.0)]


def test_plain_drill_file_has_no_slot_warning():
    path = os.path.join(os.path.dirname(__file__), "fixtures", "two_layer", "TwoLayer.drl")
    layer, _ = parse_excellon_file(path)
    assert layer.unsupported_slots == 0


class TestPermissions:
    def setup_method(self):
        self.app = Flask(__name__)
        Babel(self.app)
        self.plugin = GrblPcbPlugin()

    def test_every_command_needs_a_permission(self):
        for command in self.plugin.get_api_commands():
            assert self.plugin._required_permission(command) is not None, command

    def test_permission_per_kind_of_command(self):
        from octoprint.access.permissions import Permissions

        need = self.plugin._required_permission
        assert need("status") == Permissions.STATUS
        assert need("get_project") == Permissions.STATUS
        for command in ("jog", "home", "send_to_cnc", "start_autolevel", "probe_alignment", "spindle_on"):
            assert need(command) == Permissions.CONTROL, command
        assert need("delete_project") == Permissions.FILES_DELETE
        assert need("generate_isolation") == Permissions.FILES_UPLOAD
        assert need("download_gcode") == Permissions.FILES_DOWNLOAD

    def test_command_without_permission_is_refused(self):
        self.plugin._user_can = lambda permission: False
        called = []
        self.plugin._handle_jog = lambda data: called.append(data)
        with self.app.app_context():
            _, status = self.plugin.on_api_command("jog", {"axis": "X", "distance": 1, "feed": 100})
        assert status == 403 and not called

    @pytest.mark.parametrize("index", [-1, 5, "0", None, 1.0, True])
    def test_operation_index_is_validated(self, index):
        self.plugin._project = PCBProject()
        assert self.plugin._operation_index({"operation_index": index}) is None


def test_project_save_keeps_the_old_file_when_writing_fails(tmp_path, monkeypatch):
    project = PCBProject(name="first")
    project.save(str(tmp_path))
    path = os.path.join(project.get_project_dir(str(tmp_path)), "project.json")
    project.name = "second"

    def broken_dump(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(json, "dump", broken_dump)
    with pytest.raises(OSError):
        project.save(str(tmp_path))
    with open(path) as f:
        assert '"first"' in f.read()


# ---- travel pre-flight ------------------------------------------------------

from octoprint_grblpcb.toolpath import gcode_extent, travel_check

_LIMITS = {"travel": {"x": 200.0, "y": 200.0, "z": 50.0}, "homing": True, "soft_limits": True}


def test_gcode_extent_ignores_comments_and_offsets():
    gcode = "; X999\nG21\nG10 L20 P0 X500 Y500\nG0 X-1 Y2 ; to X700\nG1 Z-0.1 F100\nG1 X30.5 Y-4\nG1 Z5"
    assert gcode_extent(gcode) == {"x": (-1.0, 30.5), "y": (-4.0, 2.0), "z": (-0.1, 5.0)}
    assert gcode_extent("G21\nM3 S1000") is None


def test_job_inside_travel_gives_no_warning():
    extent = {"x": (0.0, 60.0), "y": (0.0, 40.0), "z": (-1.8, 5.0)}
    offset = {"x": -150.0, "y": -150.0, "z": -20.0}
    assert travel_check(extent, _LIMITS, offset, {"x": -150.0, "y": -150.0, "z": -15.0}) == []


def test_job_leaving_the_travel_is_reported():
    extent = {"x": (0.0, 60.0), "y": (0.0, 40.0)}
    offset = {"x": -30.0, "y": -150.0, "z": 0.0}  # 60 mm to the right of a zero 30 mm from the end
    warnings = travel_check(extent, _LIMITS, offset, {"x": -30.0, "y": -150.0, "z": -1.0})
    assert warnings == [{"code": "job_outside_travel", "axis": "X", "by": 30.0}]


def test_job_larger_than_the_machine_is_reported_without_homing():
    limits = dict(_LIMITS, homing=False)
    warnings = travel_check({"x": (0.0, 250.0)}, limits, {"x": 0.0}, {"x": 0.0})
    assert warnings == [{"code": "job_larger_than_travel", "axis": "X", "by": 50.0}]
    # Without homing the placement is unknown: no guess
    assert travel_check({"x": (0.0, 60.0)}, limits, {"x": 500.0}, {"x": 500.0}) == []


def test_positive_machine_space_is_recognised():
    extent = {"x": (0.0, 60.0)}
    assert travel_check(extent, _LIMITS, {"x": 100.0}, {"x": 100.0}) == []
    assert travel_check(extent, _LIMITS, {"x": 170.0}, {"x": 170.0})[0]["by"] == 30.0


def test_comm_collects_grbl_settings():
    from octoprint_grblpcb.grbl_comm import GrblComm

    comm = GrblComm("/dev/null")
    for line in ("$22=1", "$130=200.000", "$131=180.000 (y max travel, mm)", "$132=50.000", "ok"):
        comm._process_line(line)
    assert comm.settings[130] == "200.000" and comm.settings[131] == "180.000" and comm.settings[22] == "1"


# ---- drilled slots ------------------------------------------------------------

def test_g85_slot_becomes_ordered_overlapping_holes(tmp_path):
    from octoprint_grblpcb.toolpath import ToolpathSettings, generate_drill_gcode

    drill = tmp_path / "slot.drl"
    drill.write_text("M48\nMETRIC\nT1C1.000\n%\nT1\nX20.0Y20.0\nX5.0Y5.0G85X8.0Y5.0\nX1.0Y1.0\nM30\n")
    layer, drills = parse_excellon_file(str(drill))
    assert layer.drilled_slots == 1 and layer.unsupported_slots == 0
    info = drills[0]
    (first, length), = info.slot_runs
    slot = info.holes[first:first + length]
    # 3 mm with a 1 mm drill: holes every 0.5 mm, ends in the first round
    assert length == 7
    assert slot[:4] == [(5.0, 5.0), (6.0, 5.0), (7.0, 5.0), (8.0, 5.0)]
    assert slot[4:] == [(5.5, 5.0), (6.5, 5.0), (7.5, 5.0)]
    assert info.holes[first + length] == (1.0, 1.0)  # the next hole is not offset by the slot

    gcode = generate_drill_gcode(drills, ToolpathSettings(cut_depth=1.6))
    moves = [line for line in gcode.splitlines() if line.startswith("G0 X") and not line.startswith("G0 X0 Y0")]
    # Single holes first (shortest way), then the slot in its stored order
    assert moves[:2] == ["G0 X1.0000 Y1.0000", "G0 X20.0000 Y20.0000"]
    assert moves[2:] == [f"G0 X{x:.4f} Y{y:.4f}" for x, y in slot]


def test_slot_runs_survive_saving(tmp_path):
    from octoprint_grblpcb.pcb_project import DrillInfo

    project = PCBProject(name="slots")
    project.drills = [DrillInfo(tool_number=1, diameter=1.0, holes=[(0, 0), (1, 0), (0.5, 0)], count=3, slot_runs=[(0, 3)])]
    project.save(str(tmp_path))
    loaded = PCBProject.load(project.get_project_dir(str(tmp_path)))
    assert loaded.drills[0].slot_runs == [(0, 3)]
    assert loaded.drills[0].slot_indices() == {0, 1, 2}


# ---- manual commands and a running job -----------------------------------------

def test_terminal_commands_are_not_mixed_into_a_job():
    from octoprint_grblpcb.connector import ConnectedGrblPrinter

    class _Comm:
        def __init__(self):
            self.sent = []

        def send_command(self, line):
            self.sent.append(line)

    class _Log:
        def __init__(self):
            self.lines = []

        def on_printer_logs(self, line):
            self.lines.append(line)

    connector = ConnectedGrblPrinter.__new__(ConnectedGrblPrinter)
    connector._comm = _Comm()
    connector._listener = _Log()
    running = {"job": True, "paused": False}
    connector.is_streaming = lambda: running["job"]
    connector.is_paused_for_intervention = lambda: running["paused"]

    connector.commands("G0 X100")
    connector.jog({"x": 10})
    connector.home()
    assert connector._comm.sent == []
    assert len(connector._listener.lines) == 3 and all(line.startswith("!!!") for line in connector._listener.lines)

    running["paused"] = True  # tool change: the user may move the machine
    connector.commands("G0 Z5")
    assert connector._comm.sent == ["G0 Z5"]
