"""Regressions from the second UI review (server side and frontend structure)."""
import logging
import pathlib
import re

import pytest
from flask import Flask
from flask_babel import Babel

from octoprint_grblpcb import GrblPcbPlugin
from octoprint_grblpcb.pcb_project import MillingOperation, PCBProject

BASE = pathlib.Path(__file__).resolve().parents[1] / "octoprint_grblpcb"
JS = (BASE / "static" / "js" / "grblpcb.js").read_text(encoding="utf-8")
TEMPLATE = (BASE / "templates" / "grblpcb_tab.jinja2").read_text(encoding="utf-8")


def _function_body(name):
    """Source of ``self.<name> = function`` up to the next top-level ``self.`` assignment."""
    start = JS.index(f"self.{name} = function")
    nxt = re.search(r"\n        self\.[A-Za-z_]+ = ", JS[start + 10:])
    return JS[start:start + 10 + (nxt.start() if nxt else len(JS))]


class _Settings:
    def get(self, path):
        return GrblPcbPlugin().get_settings_defaults().get(path[0])

    get_int = get_float = get


@pytest.fixture
def app():
    app = Flask(__name__)
    Babel(app)
    return app


def _plugin():
    plugin = GrblPcbPlugin()
    plugin._settings = _Settings()
    plugin._logger = logging.getLogger("test")
    plugin._save_project = lambda: None
    plugin._project = PCBProject()
    plugin._project.board_width, plugin._project.board_height = 40.0, 20.0
    return plugin


# --- UI 1: machine_check ------------------------------------------------------

def test_machine_check_is_a_registered_read_command():
    from octoprint.access.permissions import Permissions
    plugin = GrblPcbPlugin()
    assert "machine_check" in plugin.get_api_commands()
    assert plugin._required_permission("machine_check") == Permissions.STATUS


# --- UI 2: rotate asks first --------------------------------------------------

def test_rotate_asks_before_discarding():
    body = _function_body("rotateProject")
    assert "_confirmDiscard" in body
    assert "projectOperations().length" in body and "hasHeightmap()" in body
    assert "_rotateProjectNow" in JS and "rotate_project" in _function_body("_rotateProjectNow")
    assert "rotate_project" not in body


# --- UI 3/4: one shared reset --------------------------------------------------

def test_new_load_delete_and_autocreate_use_the_shared_reset():
    assert "_resetProjectViewState" in _function_body("newProject")
    assert "_resetProjectViewState" in _function_body("loadProject")
    assert "_resetProjectViewState" in _function_body("_deleteProjectNow")
    assert "_resetProjectViewState" in _function_body("uploadFiles")
    reset = _function_body("_resetProjectViewState")
    for observable in ("heightmapPoints", "projectDrills", "alignmentHoles", "alignmentTransform",
                       "workspaceToolpathList", "projectOperations", "executionStatus", "projectLayers"):
        assert f"self.{observable}(" in reset, observable
    assert "updateGerberPolygons" in reset and "updateToolpaths" in reset


def test_project_update_clears_height_map_the_server_does_not_have():
    body = _function_body("_updateProjectState")
    assert "heightmapPoints([])" in body
    assert "projectOperations(projectData.operations || [])" in body


# --- UI 5: back side drawn as seen from the front -----------------------------

def test_toolpath_preview_unmirrors_back_side_operations(app):
    plugin = _plugin()
    gcode = "G0 X10 Y5 Z2\nG1 X10 Y5 Z-0.1"
    plugin._project.operations = [
        MillingOperation(name="front", operation_type="isolation", layer_type="top_copper", gcode=gcode),
        MillingOperation(name="back", operation_type="isolation", layer_type="bottom_copper", gcode=gcode,
                         mirrored=True),
    ]
    with app.app_context():
        paths = plugin._handle_get_toolpath_preview({}).get_json()["toolpaths"]
    assert paths[0]["points"][0]["x"] == 10.0 and not paths[0]["mirrored"]
    assert paths[1]["points"][0]["x"] == 30.0 and paths[1]["mirrored"]  # 40 - 10
    plugin._project.flip_axis = "Y"
    with app.app_context():
        back = plugin._handle_get_toolpath_preview({}).get_json()["toolpaths"][1]
    assert (back["points"][0]["x"], back["points"][0]["y"]) == (10.0, 15.0)  # 20 - 5


def test_mirrored_toolpaths_are_labelled_in_the_workspace():
    assert "workspaceHasMirrored" in TEMPLATE and "as seen from the front" in TEMPLATE
    assert "mirrored: !!tp.mirrored" in JS


# --- UI 6: heat map uses its own grid -----------------------------------------

def test_heat_map_grid_is_the_maps_own_not_the_input_boxes():
    sync = _function_body("_syncWorkspaceData")
    assert "updateProbeGrid" in sync
    assert "autolevelGridX" not in sync and "autolevelGridY" not in sync
    assert "heightmapGrid" in sync


# --- UI 7/8: confirmations and refresh ----------------------------------------

@pytest.mark.parametrize("name", ["startAutolevel", "setAlignmentHolesFromDrills", "clearAlignment",
                                  "applyAlignmentToAll", "computeAlignment", "applyAlignment"])
def test_actions_that_discard_gcode_ask_first(name):
    assert "_ask(" in _function_body(name) or "_confirmDiscard" in _function_body(name)


@pytest.mark.parametrize("name", ["_setupAutolevelNow", "_computeAlignmentNow", "_clearAlignmentNow"])
def test_actions_refresh_the_project_afterwards(name):
    assert "_refreshProject" in _function_body(name)


def test_clear_alignment_view_follows_the_server(app):
    plugin = _plugin()
    p = plugin._project
    p.alignment_holes = [{"expected_x": 1.0, "expected_y": 2.0, "diameter": 1.0, "probed": True,
                          "measured_x": 1.0, "measured_y": 2.0}]
    p.alignment_transform = {"tx": 1.0}
    p.alignment_probed_at = 5.0
    with app.app_context():
        plugin._handle_clear_alignment({})
    # the server keeps the selected holes but forgets measurements: the UI must not empty the list
    assert len(p.alignment_holes) == 1 and not p.alignment_holes[0]["probed"]
    assert p.alignment_transform is None
    assert "alignmentHoles([])" not in _function_body("_clearAlignmentNow")


# --- UI 9: fail handlers and server validation --------------------------------

@pytest.mark.parametrize("name", ["_setupAutolevelNow", "_applyAlignmentNow"])
def test_calls_have_fail_handlers(name):
    assert ".fail(" in _function_body(name)


@pytest.mark.parametrize("data", [
    {"grid_x": 1, "grid_y": 5, "margin": 1}, {"grid_x": 21, "grid_y": 5, "margin": 1},
    {"grid_x": "abc", "grid_y": 5, "margin": 1}, {"grid_x": 3.5, "grid_y": 5, "margin": 1},
    {"grid_x": 5, "grid_y": None, "margin": 1}, {"grid_x": 5, "grid_y": 5, "margin": -1},
    {"grid_x": 5, "grid_y": 5, "margin": "x"}, {"grid_x": 5, "grid_y": 5, "margin": float("nan")},
])
def test_setup_autolevel_rejects_bad_numbers(app, data):
    plugin = _plugin()
    with app.app_context():
        resp = plugin._handle_setup_autolevel(data)
    assert resp[1] == 400 and resp[0].get_json()["error"]
    assert plugin._height_map is None


def test_setup_autolevel_accepts_numeric_strings(app):
    plugin = _plugin()
    with app.app_context():
        body = plugin._handle_setup_autolevel({"grid_x": "4", "grid_y": 3, "margin": "1.5"}).get_json()
    assert body["total_points"] == 12
