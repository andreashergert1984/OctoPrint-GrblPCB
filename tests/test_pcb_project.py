# -*- coding: utf-8 -*-
"""Tests for the PCB project model (pcb_project.py)."""
import os

import pytest

from octoprint_grblpcb.pcb_project import (
    BoardSide,
    DrillInfo,
    LayerType,
    MillingOperation,
    PCBLayer,
    PCBProject,
    ProjectState,
    delete_project,
    guess_layer_type,
    list_projects,
)


# =========================================================================
# guess_layer_type
# =========================================================================


class TestGuessLayerType:
    """Tests for filename → LayerType guessing."""

    @pytest.mark.parametrize(
        "filename, expected",
        [
            ("board.gtl", LayerType.TOP_COPPER),
            ("board.gbl", LayerType.BOTTOM_COPPER),
            ("board.gto", LayerType.TOP_SILK),
            ("board.gbo", LayerType.BOTTOM_SILK),
            ("board.gts", LayerType.TOP_MASK),
            ("board.gbs", LayerType.BOTTOM_MASK),
            ("board.gko", LayerType.OUTLINE),
            ("board.gm1", LayerType.OUTLINE),
            ("board.drl", LayerType.DRILL),
            ("board.exc", LayerType.DRILL),
            ("board.xln", LayerType.DRILL),
        ],
    )
    def test_standard_extensions(self, filename, expected):
        assert guess_layer_type(filename) == expected

    @pytest.mark.parametrize(
        "filename, expected",
        [
            ("pcb-F_Cu.gbr", LayerType.TOP_COPPER),
            ("pcb-B_Cu.gbr", LayerType.BOTTOM_COPPER),
            ("pcb-F_Silkscreen.gbr", LayerType.TOP_SILK),
            ("pcb-B_Silkscreen.gbr", LayerType.BOTTOM_SILK),
            ("pcb-F_Mask.gbr", LayerType.TOP_MASK),
            ("pcb-B_Mask.gbr", LayerType.BOTTOM_MASK),
            ("pcb-Edge_Cuts.gbr", LayerType.OUTLINE),
        ],
    )
    def test_kicad6_compound_extensions(self, filename, expected):
        assert guess_layer_type(filename) == expected

    def test_case_insensitive(self):
        assert guess_layer_type("BOARD.GTL") == LayerType.TOP_COPPER
        assert guess_layer_type("pcb-f_cu.GBR") == LayerType.TOP_COPPER

    def test_unknown_extension(self):
        assert guess_layer_type("readme.txt") is None
        assert guess_layer_type("photo.pdf") is None
        assert guess_layer_type("data.csv") is None


# =========================================================================
# PCBLayer roundtrip
# =========================================================================


class TestPCBLayer:
    def test_to_dict_roundtrip(self):
        layer = PCBLayer(
            layer_type=LayerType.TOP_COPPER,
            filename="board.gtl",
            filepath="/tmp/board.gtl",
            min_x=1.0,
            min_y=2.0,
            max_x=51.0,
            max_y=32.0,
            width=50.0,
            height=30.0,
            parsed=True,
            svg_preview="<svg>...</svg>",
        )
        d = layer.to_dict()
        restored = PCBLayer.from_dict(d)

        assert restored.layer_type == LayerType.TOP_COPPER
        assert restored.filename == "board.gtl"
        assert restored.width == 50.0
        assert restored.parsed is True
        # svg_preview is excluded from serialization
        assert restored.svg_preview == ""

    def test_from_dict_ignores_unknown_fields(self):
        d = {
            "layer_type": "top_copper",
            "filename": "a.gtl",
            "filepath": "/a",
            "future_field": 42,
        }
        layer = PCBLayer.from_dict(d)
        assert layer.layer_type == LayerType.TOP_COPPER
        assert not hasattr(layer, "future_field")


# =========================================================================
# DrillInfo
# =========================================================================


class TestDrillInfo:
    def test_to_dict(self):
        drill = DrillInfo(
            tool_number=1,
            diameter=0.8,
            holes=[(10.0, 20.0), (30.0, 40.0)],
            count=2,
        )
        d = drill.to_dict()
        assert d["tool_number"] == 1
        assert d["diameter"] == 0.8
        assert len(d["holes"]) == 2
        assert d["count"] == 2


# =========================================================================
# MillingOperation
# =========================================================================


class TestMillingOperation:
    def test_to_dict_excludes_gcode_by_default(self):
        op = MillingOperation(
            name="Isolation Top",
            operation_type="isolation",
            layer_type="top_copper",
            gcode="G0 X0 Y0\nG1 X10 Y10\n",
        )
        d = op.to_dict(include_gcode=False)
        assert "gcode" not in d
        assert d["has_gcode"] is True
        assert d["gcode_lines"] == 3

    def test_to_dict_includes_gcode(self):
        op = MillingOperation(
            name="Drill",
            operation_type="drill",
            layer_type="drill",
            gcode="G0 X0\n",
            gcode_leveled="G0 X0\n",
        )
        d = op.to_dict(include_gcode=True)
        assert "gcode" in d
        assert "gcode_leveled" in d

    def test_from_dict_roundtrip(self):
        op = MillingOperation(
            name="Test Op",
            operation_type="isolation",
            layer_type="top_copper",
            gcode="G21\nG90\n",
            tool_diameter=0.2,
            feed_rate=600.0,
            completed=True,
        )
        d = op.to_dict(include_gcode=True)
        restored = MillingOperation.from_dict(d)
        assert restored.name == "Test Op"
        assert restored.gcode == "G21\nG90\n"
        assert restored.tool_diameter == 0.2
        assert restored.feed_rate == 600.0
        assert restored.completed is True


# =========================================================================
# PCBProject
# =========================================================================


class TestPCBProject:
    def test_new_project_has_id(self):
        p = PCBProject(name="Test")
        assert p.id
        assert len(p.id) == 8

    def test_add_layer_updates_dimensions_outline(self):
        p = PCBProject()
        outline = PCBLayer(
            layer_type=LayerType.OUTLINE,
            filename="outline.gko",
            filepath="/x",
            min_x=5.0,
            min_y=3.0,
            max_x=55.0,
            max_y=33.0,
            width=50.0,
            height=30.0,
            parsed=True,
        )
        p.add_layer(outline)
        assert p.board_width == 50.0
        assert p.board_height == 30.0
        assert p.board_min_x == 5.0
        assert p.board_min_y == 3.0

    def test_add_layer_falls_back_to_copper(self):
        p = PCBProject()
        copper = PCBLayer(
            layer_type=LayerType.TOP_COPPER,
            filename="top.gtl",
            filepath="/x",
            min_x=0.0,
            min_y=0.0,
            width=40.0,
            height=25.0,
            parsed=True,
        )
        p.add_layer(copper)
        assert p.board_width == 40.0
        assert p.board_height == 25.0

    def test_no_layers_zero_dims(self):
        p = PCBProject()
        p._update_board_dimensions()
        assert p.board_width == 0.0
        assert p.board_height == 0.0
        assert p.board_min_x == 0.0
        assert p.board_min_y == 0.0

    def test_to_dict_structure(self):
        p = PCBProject(name="Test Board")
        d = p.to_dict()
        expected_keys = {
            "id", "name", "layers", "drills", "operations", "state",
            "board_side", "board_width", "board_height", "board_min_x",
            "board_min_y", "flip_axis", "alignment_holes",
            "alignment_transform", "rotation", "has_height_map", "height_map",
        }
        assert set(d.keys()) == expected_keys

    def test_save_and_load_roundtrip(self, tmp_path):
        p = PCBProject(name="Save Test")
        layer = PCBLayer(
            layer_type=LayerType.TOP_COPPER,
            filename="board.gtl",
            filepath="/x/board.gtl",
            width=50.0,
            height=30.0,
            parsed=True,
        )
        p.add_layer(layer)
        p.drills.append(
            DrillInfo(tool_number=1, diameter=0.8, holes=[(10.0, 20.0)], count=1)
        )
        p.operations.append(
            MillingOperation(
                name="Iso",
                operation_type="isolation",
                layer_type="top_copper",
                gcode="G21\nG90\n",
            )
        )
        p.rotation = 90.0
        p.alignment_holes = [{"expected_x": 5, "expected_y": 5}]

        base_dir = str(tmp_path)
        p.save(base_dir)

        loaded = PCBProject.load(os.path.join(base_dir, p.id))
        assert loaded.id == p.id
        assert loaded.name == "Save Test"
        assert LayerType.TOP_COPPER in loaded.layers
        assert len(loaded.drills) == 1
        assert loaded.drills[0].diameter == 0.8
        assert len(loaded.operations) == 1
        assert loaded.operations[0].gcode == "G21\nG90\n"
        assert loaded.rotation == 90.0
        assert loaded.alignment_holes == [{"expected_x": 5, "expected_y": 5}]

    def test_load_without_gcode(self, tmp_path):
        p = PCBProject(name="No GCode")
        p.operations.append(
            MillingOperation(
                name="Op",
                operation_type="drill",
                layer_type="drill",
                gcode="G21\n",
            )
        )
        base_dir = str(tmp_path)
        p.save(base_dir)

        loaded = PCBProject.load(
            os.path.join(base_dir, p.id), include_gcode=False
        )
        assert loaded.operations[0].gcode == ""
        assert loaded.operations[0].name == "Op"


# =========================================================================
# list_projects / delete_project
# =========================================================================


class TestProjectManagement:
    def test_list_projects_empty_dir(self, tmp_path):
        assert list_projects(str(tmp_path)) == []

    def test_list_projects_nonexistent(self, tmp_path):
        assert list_projects(str(tmp_path / "nope")) == []

    def test_list_and_delete_projects(self, tmp_path):
        base = str(tmp_path)
        p1 = PCBProject(name="Project A")
        p2 = PCBProject(name="Project B")
        p1.save(base)
        p2.save(base)

        projects = list_projects(base)
        assert len(projects) == 2
        names = {p["name"] for p in projects}
        assert names == {"Project A", "Project B"}

        # Delete one
        assert delete_project(base, p1.id) is True
        assert len(list_projects(base)) == 1

    def test_delete_path_traversal_blocked(self, tmp_path):
        base = str(tmp_path)
        assert delete_project(base, "../etc") is False

    def test_delete_nonexistent(self, tmp_path):
        assert delete_project(str(tmp_path), "nope") is False
