# -*- coding: utf-8 -*-
"""Integration tests for routing and drilling sample files."""

from pathlib import Path

from octoprint_grblpcb.gerber_parser import (
    merge_geometries,
    parse_excellon_file,
    parse_gerber_file,
)
from octoprint_grblpcb.toolpath import (
    ToolpathSettings,
    generate_drill_gcode,
    generate_isolation_gcode,
)


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def test_sample_gerber_and_drill_fixtures_roundtrip():
    gerber_path = FIXTURES_DIR / "sample_top_copper.gbr"
    drill_path = FIXTURES_DIR / "sample_drill.drl"

    layer, geometries = parse_gerber_file(str(gerber_path))
    drill_layer, drills = parse_excellon_file(str(drill_path))

    copper = merge_geometries(geometries)
    settings = ToolpathSettings()
    isolation_gcode = generate_isolation_gcode(copper, settings)
    drill_gcode = generate_drill_gcode(drills, settings)

    assert layer.parsed is True
    assert layer.filename == "sample_top_copper.gbr"
    assert len(geometries) == 2
    assert round(layer.width, 3) == 1.500
    assert round(layer.height, 3) == 0.500
    assert round(copper.bounds[0], 3) == 0.750
    assert round(copper.bounds[2], 3) == 2.250

    assert drill_layer.parsed is True
    assert drill_layer.filename == "sample_drill.drl"
    assert len(drills) == 1
    assert drills[0].count == 2
    assert round(drill_layer.width, 3) == 1.000
    assert round(drill_layer.height, 3) == 1.000

    assert "M2" in isolation_gcode
    assert "M2" in drill_gcode
    assert "G21" in isolation_gcode
    assert "G21" in drill_gcode
    assert "M0" in drill_gcode