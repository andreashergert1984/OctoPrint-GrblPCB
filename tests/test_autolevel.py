"""Tests for autoleveling: height map, interpolation, G-code Z-correction."""

import logging
import queue

import pytest

from octoprint_grblpcb import GrblPcbPlugin
from octoprint_grblpcb.autolevel import (
    HeightMap,
    ProbePoint,
    _extract_feed,
    _parse_xyz,
    _rebuild_gcode_line,
    _subdivide_segment,
    apply_autolevel,
    height_map_side_check,
)

# =========================================================================
# ProbePoint
# =========================================================================


class TestProbePoint:
    def test_to_dict_roundtrip(self):
        p = ProbePoint(x=10.5, y=20.3, z=-0.15, probed=True)
        d = p.to_dict()
        r = ProbePoint.from_dict(d)
        assert r.x == 10.5
        assert r.y == 20.3
        assert r.z == -0.15
        assert r.probed is True


# =========================================================================
# HeightMap grid generation
# =========================================================================


class TestHeightMapGrid:
    def test_grid_dimensions(self):
        hm = HeightMap()
        hm.generate_grid(0, 0, 50, 30, grid_x=5, grid_y=3, margin=1.0)
        assert len(hm.points) == 15  # 5 × 3
        assert hm.min_x == -1.0
        assert hm.min_y == -1.0
        assert hm.max_x == 51.0
        assert hm.max_y == 31.0

    def test_serpentine_order(self):
        hm = HeightMap()
        hm.generate_grid(0, 0, 10, 10, grid_x=3, grid_y=3, margin=0)
        # Row 0 (even) → left to right
        assert hm.points[0].x < hm.points[1].x < hm.points[2].x
        # Row 1 (odd) → right to left
        assert hm.points[3].x > hm.points[4].x > hm.points[5].x
        # Row 2 (even) → left to right
        assert hm.points[6].x < hm.points[7].x < hm.points[8].x

    def test_probe_result_and_completion(self):
        hm = HeightMap()
        hm.generate_grid(0, 0, 10, 10, grid_x=2, grid_y=2, margin=0)
        assert not hm.is_complete()

        for i in range(4):
            hm.set_probe_result(i, -0.1 * i)

        assert hm.is_complete()
        assert hm.points[2].z == pytest.approx(-0.2)

    def test_next_unprobed_index(self):
        hm = HeightMap()
        hm.generate_grid(0, 0, 10, 10, grid_x=2, grid_y=2, margin=0)
        assert hm.next_unprobed_index() == 0
        hm.set_probe_result(0, 0.0)
        assert hm.next_unprobed_index() == 1
        for i in range(1, 4):
            hm.set_probe_result(i, 0.0)
        assert hm.next_unprobed_index() == -1


# =========================================================================
# HeightMap interpolation
# =========================================================================


def _make_completed_heightmap(grid_x=3, grid_y=3, z_func=None):
    """Helper: create a completed height map with z_func(x, y) for each point."""
    if z_func is None:
        def z_func(x, y):
            return 0.0
    hm = HeightMap()
    hm.generate_grid(0, 0, 10, 10, grid_x=grid_x, grid_y=grid_y, margin=0)
    for i, p in enumerate(hm.points):
        hm.set_probe_result(i, z_func(p.x, p.y))
    hm.build_interpolator()
    return hm


class TestHeightMapInterpolation:
    def test_flat_surface(self):
        hm = _make_completed_heightmap(z_func=lambda x, y: 0.0)
        assert abs(hm.get_z(5.0, 5.0)) < 1e-6
        assert abs(hm.get_z(0.0, 0.0)) < 1e-6

    def test_tilted_surface(self):
        """Z = 0.01x + 0.02y — bilinear should reproduce exactly."""
        hm = _make_completed_heightmap(
            grid_x=3, grid_y=3,
            z_func=lambda x, y: 0.01 * x + 0.02 * y,
        )
        assert hm.get_z(5.0, 5.0) == pytest.approx(0.15, abs=0.01)
        assert hm.get_z(10.0, 10.0) == pytest.approx(0.30, abs=0.01)

    def test_no_interpolator_returns_zero(self):
        hm = HeightMap()
        hm.generate_grid(0, 0, 10, 10, grid_x=3, grid_y=3, margin=0)
        # Not probed → no interpolator
        assert hm.get_z(5.0, 5.0) == 0.0

    def test_clamped_to_bounds(self):
        """Points outside grid are clamped (fill_value=None extrapolates)."""
        hm = _make_completed_heightmap(z_func=lambda x, y: 0.1)
        # Should not crash for out-of-bounds query
        z = hm.get_z(100.0, 100.0)
        assert isinstance(z, float)


# =========================================================================
# HeightMap serialization
# =========================================================================


class TestHeightMapSerialization:
    def test_roundtrip(self):
        hm = _make_completed_heightmap(
            z_func=lambda x, y: 0.01 * x + 0.02 * y
        )
        d = hm.to_dict()

        restored = HeightMap.from_dict(d)
        assert restored.completed is True
        assert len(restored.points) == len(hm.points)
        # Interpolator should auto-rebuild
        assert restored.get_z(5.0, 5.0) == pytest.approx(
            hm.get_z(5.0, 5.0), abs=0.001
        )


# =========================================================================
# G-code parsing helpers
# =========================================================================


class TestParseXYZ:
    def test_full_line(self):
        x, y, z = _parse_xyz("G1 X10.5 Y20.3 Z-0.1", 0, 0, 0)
        assert x == pytest.approx(10.5)
        assert y == pytest.approx(20.3)
        assert z == pytest.approx(-0.1)

    def test_defaults(self):
        x, y, z = _parse_xyz("G1 X5", 1.0, 2.0, 3.0)
        assert x == pytest.approx(5.0)
        assert y == pytest.approx(2.0)  # default
        assert z == pytest.approx(3.0)  # default


class TestExtractFeed:
    def test_present(self):
        assert _extract_feed("G1 X10 Y20 F500") == "500"

    def test_absent(self):
        assert _extract_feed("G1 X10 Y20") is None


class TestRebuildGcodeLine:
    def test_g0(self):
        result = _rebuild_gcode_line("G0 X1 Y2 Z3", 10.0, 20.0, 30.0)
        assert result == "G0 X10.0000 Y20.0000 Z30.0000"

    def test_g1_with_feed(self):
        result = _rebuild_gcode_line("G1 X1 Y2 Z3 F500", 10.0, 20.0, -0.1)
        assert "G1" in result
        assert "F500" in result
        assert "Z-0.1000" in result


# =========================================================================
# Segment subdivision
# =========================================================================


class TestSubdivideSegment:
    def test_short_segment(self):
        """Segment shorter than max_length returns single endpoint."""
        result = _subdivide_segment(0, 0, 0, 1, 0, 0, max_length=5.0)
        assert len(result) == 1
        assert result[0] == pytest.approx((1.0, 0.0, 0.0))

    def test_long_segment(self):
        """10mm segment with max_length=3: ceil(10/3)=4 sub-segments."""
        result = _subdivide_segment(0, 0, 0, 10, 0, 0, max_length=3.0)
        assert len(result) == 4
        # Last point should be the endpoint
        assert result[-1][0] == pytest.approx(10.0)

    def test_z_interpolation(self):
        """Z is interpolated linearly."""
        result = _subdivide_segment(0, 0, 0, 10, 0, -1.0, max_length=5.0)
        assert len(result) == 2
        assert result[0][2] == pytest.approx(-0.5)
        assert result[1][2] == pytest.approx(-1.0)

    def test_degenerate_zero_length(self):
        result = _subdivide_segment(5, 5, -0.1, 5, 5, -0.1, max_length=1.0)
        assert len(result) == 1


# =========================================================================
# apply_autolevel
# =========================================================================


class TestApplyAutolevel:
    def test_flat_surface_no_change(self):
        hm = _make_completed_heightmap(z_func=lambda x, y: 0.0)
        gcode = "G0 Z5.000\nG0 X5.0000 Y5.0000\nG1 Z-0.1000 F100\nG1 X10.0000 Y5.0000"
        result = apply_autolevel(gcode, hm)
        # Z correction is 0 everywhere → Z values should be unchanged
        assert "Z-0.1000" in result

    def test_tilted_surface_adds_correction(self):
        hm = _make_completed_heightmap(
            z_func=lambda x, y: 0.1  # uniform 0.1mm surface offset
        )
        gcode = "G0 Z5.000\nG0 X5.0000 Y5.0000\nG1 Z-0.1000 F100"
        result = apply_autolevel(gcode, hm)
        # Z = -0.1 + 0.1 correction = 0.0
        lines = result.splitlines()
        # The G1 line should be corrected
        corrected_z_lines = [line for line in lines if line.startswith("G1") and "Z" in line]
        assert len(corrected_z_lines) > 0

    def test_preserves_comments(self):
        hm = _make_completed_heightmap()
        gcode = "; header comment\n\nM3 S10000\nG1 X5 Y5 Z-0.1 F100"
        result = apply_autolevel(gcode, hm)
        lines = result.splitlines()
        assert lines[0] == "; header comment"
        assert lines[1] == ""
        assert lines[2] == "M3 S10000"

    def test_rapid_above_surface_not_corrected(self):
        hm = _make_completed_heightmap(z_func=lambda x, y: 0.1)
        gcode = "G0 X5.0000 Y5.0000\nG0 Z5.0000"
        result = apply_autolevel(gcode, hm)
        # Z=5 is above surface, should not get correction
        assert "Z5.0000" in result


# =========================================================================
# Probe result handling: never record substitute heights
# =========================================================================


class _Comm:
    def __init__(self, *responses):
        self._response_queue = queue.Queue()
        for r in responses:
            self._response_queue.put(r)


def _plugin():
    plugin = GrblPcbPlugin()
    plugin.PROBE_TIMEOUT = 0.2
    return plugin


def test_probe_result_in_work_coordinates():
    comm = _Comm(("probe", {"x": 1.0, "y": 2.0, "z": -1.2, "success": True}), ("ok",))
    assert _plugin()._await_probe_result(comm, {"z": -1.0}) == (pytest.approx(-0.2), None)


@pytest.mark.parametrize(
    "responses, reason",
    [
        ([("probe", {"x": 0.0, "y": 0.0, "z": -5.0, "success": False})], "no_contact"),
        ([("error", 9, "locked")], "grbl_error"),
        ([("alarm", 5, "Probe fail")], "alarm"),
        ([], "timeout"),
    ],
)
def test_probe_failures_give_no_height(responses, reason):
    assert _plugin()._await_probe_result(_Comm(*responses), {"z": 0.0}) == (None, reason)


def test_user_stop_is_not_a_failure():
    plugin = _plugin()
    plugin._probe_stop.set()
    assert plugin._await_probe_result(_Comm(), {"z": 0.0}) == (None, None)


class _ProbeComm:
    def __init__(self):
        self._response_queue = queue.Queue()
        self._stream_mode = False


class _ProbeConnector:
    """Answers moves with ok; probe i answers with probe_answers[i]."""

    def __init__(self, probe_answers):
        self._comm = _ProbeComm()
        self._work_offset = {"x": 0.0, "y": 0.0, "z": 0.0}
        self._work_position = {"x": 0.0, "y": 0.0, "z": 0.0}
        self._probe_answers = list(probe_answers)

    def commands(self, *commands, **kwargs):
        for command in commands:
            if command.startswith("G38.2"):
                answer = self._probe_answers.pop(0) if self._probe_answers else []
                for response in answer:
                    self._comm._response_queue.put(response)
            else:
                self._comm._response_queue.put(("ok",))


class _Settings:
    def get_float(self, path):
        return 1.0


class _PluginManager:
    def __init__(self):
        self.messages = []

    def send_plugin_message(self, plugin, data):
        self.messages.append(data)


def _contact(z):
    return [("probe", {"x": 0.0, "y": 0.0, "z": z, "success": True}), ("ok",)]


@pytest.mark.parametrize(
    "failure, reason",
    [
        ([("probe", {"x": 0.0, "y": 0.0, "z": -5.0, "success": False}), ("alarm", 5, "Probe fail")], "no_contact"),
        ([("error", 9, "locked")], "grbl_error"),
        ([("alarm", 5, "Probe fail")], "alarm"),
        ([], "timeout"),
    ],
)
def test_probe_loop_stops_without_recording_failed_point(failure, reason):
    from octoprint_grblpcb.pcb_project import PCBProject

    plugin = _plugin()
    plugin._logger = logging.getLogger("test")
    class FastProbe(_Settings):
        def get_float(self, path):
            return 1e9 if path[0] == "probe_feed" else 1.0

    plugin._settings = FastProbe()
    # the wait follows travel and feed; a huge feed leaves only the slack
    plugin.PROBE_SLACK = 0.2
    plugin._plugin_manager = _PluginManager()
    plugin._save_project = lambda: None
    plugin._project = PCBProject()
    plugin._height_map = HeightMap()
    plugin._height_map.generate_grid(0, 0, 10, 10, grid_x=2, grid_y=2)

    connector = _ProbeConnector([_contact(-0.1), failure, _contact(-0.2), _contact(-0.3)])
    plugin._autolevel_probe_loop(connector)

    points = plugin._height_map.points
    assert points[0].probed and points[0].z == pytest.approx(-0.1)
    assert not points[1].probed
    assert not any(p.probed for p in points[2:])
    failed = [m for m in plugin._plugin_manager.messages if m["type"] == "autolevel_failed"]
    assert failed == [{"type": "autolevel_failed", "index": 1, "total": 4, "reason": reason}]


# =========================================================================
# Height map side: front vs back after flipping
# =========================================================================




def _map_probed_at(t):
    hm = HeightMap()
    hm.probed_at = t
    return hm


@pytest.mark.parametrize(
    "probed_at, mirrored, alignment_at, expected",
    [
        (200.0, True, 100.0, None),                      # back side, measured after flip
        (50.0, True, 100.0, "heightmap_from_front"),     # back side, old front map
        (50.0, True, None, "heightmap_side_unknown"),    # back side, flip never recorded
        (None, True, 100.0, "heightmap_side_unknown"),   # map from before this check
        (50.0, False, 100.0, None),                      # front side, measured before flip
        (200.0, False, 100.0, "heightmap_from_back"),    # front side, back map
        (200.0, False, None, None),                      # single-sided job
    ],
)
def test_height_map_side_check(probed_at, mirrored, alignment_at, expected):
    result = height_map_side_check(_map_probed_at(probed_at), mirrored, alignment_at)
    assert (result or {}).get("code") == expected


# =========================================================================
# Starting again: resume vs. measure anew
# =========================================================================

class _StartConnector:
    def __init__(self, work_offset):
        self._work_offset = work_offset

    def is_streaming(self):
        return False


def _start(plugin, connector):
    from flask import Flask
    from flask_babel import Babel

    app = Flask(__name__)
    Babel(app)
    plugin._get_grbl_connector = lambda: connector
    plugin._autolevel_probe_loop = lambda c: None  # only the start decision is tested
    with app.app_context():
        plugin._handle_start_autolevel({})
    plugin._probe_thread.join()


def _start_plugin(probed):
    from octoprint_grblpcb.pcb_project import PCBProject

    plugin = _plugin()
    plugin._save_project = lambda: None
    plugin._project = PCBProject()
    hm = HeightMap()
    hm.generate_grid(0, 0, 10, 10, grid_x=2, grid_y=2, margin=0)
    for i in probed:
        hm.set_probe_result(i, -0.3)
    hm.work_offset = {"x": 10.0, "y": 10.0, "z": -1.0}
    hm.probed_at = 100.0
    plugin._height_map = hm
    return plugin


def test_complete_map_is_measured_again():
    plugin = _start_plugin(probed=[0, 1, 2, 3])
    _start(plugin, _StartConnector({"x": 10.0, "y": 10.0, "z": -1.0}))
    hm = plugin._height_map
    assert not any(p.probed for p in hm.points)
    assert hm.probed_at > 100.0


def test_interrupted_run_on_same_zero_resumes():
    plugin = _start_plugin(probed=[0, 1])
    _start(plugin, _StartConnector({"x": 10.0, "y": 10.0, "z": -0.5}))
    hm = plugin._height_map
    assert [p.probed for p in hm.points] == [True, True, False, False]
    assert hm.probed_at == 100.0


def test_interrupted_run_with_moved_zero_starts_over():
    plugin = _start_plugin(probed=[0, 1])
    _start(plugin, _StartConnector({"x": 25.0, "y": 10.0, "z": -1.0}))
    hm = plugin._height_map
    assert not any(p.probed for p in hm.points)
    assert hm.work_offset["x"] == 25.0
