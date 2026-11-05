# -*- coding: utf-8 -*-
"""Regression tests for GrblPCB UI template wiring."""

from pathlib import Path


def test_grblpcb_tab_template_does_not_duplicate_tab_root_id():
    template_path = (
        Path(__file__).resolve().parents[1]
        / "octoprint_grblpcb"
        / "templates"
        / "grblpcb_tab.jinja2"
    )

    content = template_path.read_text(encoding="utf-8")

    assert 'id="tab_plugin_grblpcb"' not in content


def test_grblpcb_tab_template_includes_work_coordinate_help():
    template_path = (
        Path(__file__).resolve().parents[1]
        / "octoprint_grblpcb"
        / "templates"
        / "grblpcb_tab.jinja2"
    )

    content = template_path.read_text(encoding="utf-8")

    assert "G54-G59" in content
    assert "current work coordinate system" in content.lower()
    assert "G0" in content and "G1" in content
