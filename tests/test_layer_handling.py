"""Layer removal, stale operations, board extent and drill holes for the canvas."""
import logging

import pytest
from flask import Flask
from flask_babel import Babel

from octoprint_grblpcb import GrblPcbPlugin
from octoprint_grblpcb.pcb_project import (
    DrillInfo,
    LayerType,
    MillingOperation,
    PCBLayer,
    PCBProject,
)


def _layer(layer_type, min_x, min_y, max_x, max_y):
    return PCBLayer(layer_type=layer_type, filename=f"{layer_type.value}.gbr", filepath="", parsed=True,
                    min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y,
                    width=max_x - min_x, height=max_y - min_y)


def _plugin():
    plugin = GrblPcbPlugin()
    plugin._save_project = lambda: None
    plugin._logger = logging.getLogger("test")
    plugin._project = PCBProject()
    return plugin


@pytest.fixture
def app():
    app = Flask(__name__)
    Babel(app)
    return app


def test_board_without_outline_is_the_union_of_the_copper_layers():
    project = PCBProject()
    project.add_layer(_layer(LayerType.TOP_COPPER, 5.0, 0.0, 50.0, 20.0))
    project.add_layer(_layer(LayerType.BOTTOM_COPPER, 2.0, 1.0, 40.0, 30.0))
    assert (project.board_min_x, project.board_min_y) == (2.0, 0.0)
    assert (project.board_width, project.board_height) == (48.0, 30.0)


def test_removing_the_drill_layer_removes_holes_and_drill_operations(app):
    plugin = _plugin()
    p = plugin._project
    p.add_layer(_layer(LayerType.TOP_COPPER, 0.0, 0.0, 50.0, 30.0))
    p.add_layer(_layer(LayerType.DRILL, 5.0, 5.0, 45.0, 25.0))
    p.drills = [DrillInfo(tool_number=1, diameter=0.8, holes=[(5.0, 5.0)], count=1)]
    p.operations = [
        MillingOperation(name="Drill", operation_type="drill", layer_type="drill", gcode="G0"),
        MillingOperation(name="Iso", operation_type="isolation", layer_type="top_copper", gcode="G0"),
    ]
    with app.app_context():
        plugin._handle_remove_layer({"layer_type": "drill"})
    assert p.drills == []
    assert [op.layer_type for op in p.operations] == ["top_copper"]


def test_removing_the_outline_drops_all_operations_because_the_origin_moves(app):
    plugin = _plugin()
    p = plugin._project
    p.add_layer(_layer(LayerType.TOP_COPPER, 5.0, 5.0, 45.0, 25.0))
    p.add_layer(_layer(LayerType.OUTLINE, 0.0, 0.0, 50.0, 30.0))
    p.operations = [MillingOperation(name="Iso", operation_type="isolation", layer_type="top_copper", gcode="G0")]
    with app.app_context():
        plugin._handle_remove_layer({"layer_type": "outline"})
    assert p.board_min_x == 5.0
    assert p.operations == []


def test_layer_geometry_includes_drill_holes_relative_to_the_board(app):
    plugin = _plugin()
    p = plugin._project
    p.add_layer(_layer(LayerType.OUTLINE, 10.0, 20.0, 60.0, 50.0))
    p.drills = [DrillInfo(tool_number=1, diameter=3.0, holes=[(14.0, 24.0)], count=1)]
    with app.app_context():
        body = plugin._handle_get_layer_geometry({}).get_json()
    assert body["holes"] == [[4.0, 4.0, 3.0]]


def test_unmirrored_bottom_operation_is_marked_outdated():
    old = MillingOperation(name="Iso", operation_type="isolation", layer_type="bottom_copper", gcode="G0")
    new = MillingOperation(name="Iso", operation_type="isolation", layer_type="bottom_copper", gcode="G0",
                           mirrored=True)
    assert old.to_dict()["outdated"] is True
    assert new.to_dict()["outdated"] is False
    assert MillingOperation(name="D", operation_type="drill", layer_type="drill").to_dict()["outdated"] is False


def test_rename_project(app):
    plugin = _plugin()
    with app.app_context():
        body = plugin._handle_rename_project({"name": "  Two-layer demo  "}).get_json()
        _response, status = plugin._handle_rename_project({"name": "   "})
    assert body["project"]["name"] == "Two-layer demo"
    assert plugin._project.name == "Two-layer demo"
    assert status == 400
