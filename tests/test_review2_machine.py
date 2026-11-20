"""Regressions from the second machine-control review (tool change height,
absolute probing, goto zero, hole plunge, stop during the drift check, silent
link, page reload during a job, probe lock-outs, halt races, WCS, z_zeroed)."""
import logging
import pathlib
import queue
import re
import threading
import time

import pytest
import test_stream_pauses as tsp
from flask import Flask
from flask_babel import Babel

from octoprint_grblpcb import GrblPcbPlugin
from octoprint_grblpcb.autolevel import HeightMap
from octoprint_grblpcb.pcb_project import PCBProject
from octoprint_grblpcb.toolpath import ToolpathSettings, generate_drill_gcode

BASE = pathlib.Path(__file__).resolve().parents[1] / "octoprint_grblpcb"
JS = (BASE / "static" / "js" / "grblpcb.js").read_text(encoding="utf-8")


@pytest.fixture
def app():
    app = Flask(__name__)
    Babel(app)
    return app


class _S:
    V = {  # noqa: RUF012
        "probe_depth": -3.0, "probe_feed": 300.0, "safe_z_height": 5.0,
         "travel_z_height": 2.0, "z_max_feed": 600.0, "board_thickness": 1.6}

    def get_float(self, p):
        return self.V[p[0]]

    def get_int(self, p):
        return int(self.V[p[0]])


class _PM:
    def __init__(self):
        self.messages = []

    def send_plugin_message(self, plugin, data):
        self.messages.append(data)


def _plugin(connector=None):
    plugin = GrblPcbPlugin()
    plugin._settings = _S()
    plugin._save_project = lambda: None
    plugin._logger = logging.getLogger("review2-machine")
    plugin._user_can = lambda permission: True
    plugin._plugin_manager = _PM()
    plugin._project = PCBProject()
    plugin._get_grbl_connector = lambda: connector
    return plugin


def _idle(c, t=20):
    time.sleep(0.4)
    end = time.time() + t
    while time.time() < end and c._grbl_state != "Idle":
        time.sleep(0.05)


# ---------------------------------------------------------------- 1: lift after M6/M0

def test_job_lifts_to_safe_z_before_xy_travel_after_a_tool_change():
    c, received, sim, cleanup = tsp._make_setup(0.5)
    try:
        gcode = "\n".join([  # noqa: FLY002
            "G21", "G90", "G54", "G1 Z5.000 F200", "M5", "T1 M6 ; drill",
            "M3 S10000", "G4 P0.1", "G0 X20.0000 Y20.0000", "G1 Z1.000 F200", "M5", "M2",
        ])
        assert c.stream_gcode(gcode, "drill")
        assert tsp._wait_status(c, "tool_change", 10)
        _idle(c)
        # the user probes and zeroes Z at the board: the tool stands at Z 0
        c.probe("Z", -10.0, 200.0, zero=True, clearance=0.0)
        end = time.time() + 20
        while not any(m.get("type") == "probe_zero_result" for m in c._plugin_manager.messages) \
                and time.time() < end:
            time.sleep(0.05)
        _idle(c)
        assert sim.wpos()["z"] == pytest.approx(0.0, abs=0.05)
        samples = []
        stop = threading.Event()

        def sampler():
            while not stop.is_set():
                samples.append((dict(sim.mpos), sim.spindle_mode, sim.wpos()["z"]))
                time.sleep(0.01)

        threading.Thread(target=sampler, daemon=True).start()
        c.confirm_tool_change()
        assert tsp._wait_status(c, "completed", 40)
        stop.set()
        moving_xy = [s for s in samples if 0.5 < s[0]["x"] < 19.5]
        assert moving_xy, "no XY travel observed"
        assert all(s[2] >= 4.9 for s in moving_xy), moving_xy[:3]
        # the lift goes out before any spindle restart or XY word
        i = received.index("G0 X20.0000 Y20.0000")
        assert any(line.startswith("G21 G90 G54 G1 Z5.000") for line in received[:i][-6:])
    finally:
        cleanup()


def test_pause_restore_lifts_before_the_spindle_restarts():
    c, received, _sim, cleanup = tsp._make_setup(0.0)
    try:
        assert c.stream_gcode("G21\nG90\nM3 S8000\nG1 X1 F300\nM0\nG1 X2\n", operation_name="t")
        assert tsp._wait_status(c, "pause")
        c.confirm_tool_change()
        assert tsp._wait_status(c, "completed", timeout=10)
        i = received.index("G1 X2")
        tail = received[i - 4:i]
        assert tail[0].startswith("G21 G90 G54 G1 Z5.000")
        assert tail[2] == "M3 S8000"
    finally:
        cleanup()


def test_generated_drill_code_goes_to_safe_z_after_the_tool_change():
    from octoprint_grblpcb.gerber_parser import DrillInfo
    drills = [DrillInfo(tool_number=1, diameter=0.8, holes=[(1.0, 1.0), (3.0, 2.0)])]
    gcode = generate_drill_gcode(drills, ToolpathSettings(cut_depth=1.6, safe_z=5))
    lines = [ln.split(";")[0].strip() for ln in gcode.splitlines()]
    lines = [ln for ln in lines if ln]
    m6 = next(i for i, ln in enumerate(lines) if "M6" in ln)
    first_xy = next(i for i, ln in enumerate(lines) if i > m6 and re.match(r"G0 X", ln))
    between = lines[m6 + 1:first_xy]
    assert any(ln.startswith(("G1 Z5", "G0 Z5")) for ln in between), between
    lift = next(i for i, ln in enumerate(between) if ln.startswith(("G1 Z5", "G0 Z5")))
    spindle = next(i for i, ln in enumerate(between) if ln.startswith("M3"))
    assert lift < spindle, between  # lift first, then the spindle starts


# ---------------------------------------------------------------- 2: absolute probing

def test_probe_never_uses_relative_mode_and_leaves_g90_after_a_failure():
    c, received, sim, cleanup = tsp._make_setup(0.05)
    try:
        c.commands("G21 G90", "G0 X30 Y30")
        _idle(c)
        c.zero_axis("all")
        time.sleep(0.3)
        c.probe("Z", -0.5, 300.0, zero=True, clearance=5.0)
        time.sleep(2.0)
        assert c._grbl_state == "Alarm"
        assert "G91" not in received
        assert sim.absolute
        c.home()
        _idle(c)
        assert c._grbl_state == "Idle"
        assert sim.absolute  # $H is followed by G90

        plugin = _plugin(c)
        plugin._probe_wcs = "G54"
        hm = HeightMap()
        hm.generate_grid(0, 0, 20, 20, grid_x=2, grid_y=2, margin=0)
        plugin._height_map = hm
        probes = []
        orig = sim._probe

        def rec(target, fail_ok):
            sim._planner.join()
            probes.append(dict(sim.wpos()))
            return orig(target, fail_ok)

        sim._probe = rec
        plugin._autolevel_probe_loop(c)
        first = hm.points[0]
        assert probes[0]["x"] == pytest.approx(first.x, abs=0.01)
        assert probes[0]["y"] == pytest.approx(first.y, abs=0.01)
    finally:
        cleanup()


def test_manual_probe_targets_are_absolute_from_the_current_position():
    c, received, _sim, cleanup = tsp._make_setup(0.0)
    try:
        c.commands("G21 G90", "G0 Z4")
        _idle(c)
        c.probe("Z", -10.0, 100.0)
        end = time.time() + 10
        while c.is_probe_zero_running() and time.time() < end:
            time.sleep(0.05)
        g38 = [line for line in received if line.startswith("G38.2")]
        assert g38[0].startswith("G38.2 Z-6.000")  # 4 mm - 10 mm, absolute
        assert not any(line.startswith("G91") for line in received)
    finally:
        cleanup()


class _ProbeComm:
    def __init__(self):
        self._response_queue = queue.Queue()
        self._stream_mode = False


class _AutolevelConn:
    """Every command answers ok; a G38.2 answers a contact and ok."""

    _grbl_state = "Idle"

    def __init__(self):
        self._comm = _ProbeComm()
        self._work_offset = {"x": 0.0, "y": 0.0, "z": 0.0}
        self.sent = []

    def commands(self, *commands, **kwargs):
        for command in commands:
            self.sent.append(command)
            if command.startswith("G38.2"):
                self._comm._response_queue.put(("probe", {"x": 0.0, "y": 0.0, "z": -0.1, "success": True}))
            self._comm._response_queue.put(("ok",))


def test_every_probe_loop_move_block_starts_with_absolute_mode():
    conn = _AutolevelConn()
    plugin = _plugin(conn)
    plugin._probe_wcs = "G55"
    hm = HeightMap()
    hm.generate_grid(0, 0, 20, 20, grid_x=2, grid_y=2, margin=0)
    plugin._height_map = hm
    plugin._autolevel_probe_loop(conn)
    assert hm.is_complete()
    for i, command in enumerate(conn.sent):
        # (the final "G0 X0 Y0" after the run's closing retract is not a move block)
        if command.startswith("G0 X") and i >= 2 and conn.sent[i - 1].startswith("G1 Z") \
                and not conn.sent[i - 2].startswith("G1 Z"):
            assert conn.sent[i - 2] == "G21 G90 G55", conn.sent[i - 3:i + 1]
        if command.startswith("G38.2"):
            assert conn.sent[i - 1] == "G21 G90 G55"
    assert not any("G91" in c for c in conn.sent)


def test_autolevel_against_the_simulator_still_completes():
    c, _received, sim, cleanup = tsp._make_setup(0.0)
    try:
        plugin = _plugin(c)
        hm = HeightMap()
        hm.generate_grid(0, 0, 20, 20, grid_x=3, grid_y=3, margin=0)
        plugin._height_map = hm
        plugin._autolevel_probe_loop(c)
        assert hm.is_complete()
        done = [m for m in plugin._plugin_manager.messages if m.get("type") == "autolevel_complete"]
        assert len(done) == 1 and "z_drift" in done[0]
        assert sim.absolute
    finally:
        cleanup()


# ---------------------------------------------------------------- 3: go to 0,0

def test_goto_zero_stops_at_safe_z_above_the_zero(app):
    calls = []

    class Conn:
        _grbl_state = "Idle"
        _work_position = {"x": 3.0, "y": 4.0, "z": 0.0}  # noqa: RUF012

        def is_streaming(self):
            return False

        def accepts_manual_commands(self):
            return True

        def commands(self, *commands, **kwargs):
            calls.extend(commands)

    plugin = _plugin(Conn())
    with app.app_context():
        plugin.on_api_command("goto_zero", {})
    assert calls[0] == "G21 G90"
    assert calls[1].startswith("G1 Z5")
    assert calls[-1] == "G0 X0 Y0"
    assert not any(re.match(r"G[01] Z-?0(\.0+)?\b", c) for c in calls)  # no lowering onto the board


# ---------------------------------------------------------------- 4: hole plunge

class _HoleConn:
    _grbl_state = "Idle"

    def __init__(self, touch):
        self._comm = _ProbeComm()
        self._work_offset = {"x": 0.0, "y": 0.0, "z": 0.0}
        self.sent = []
        self.touch = touch

    def commands(self, *commands, **kwargs):
        for command in commands:
            self.sent.append(command)
            if command.startswith("G38.3"):
                hit = self.touch and " Z" in command
                self._comm._response_queue.put(
                    ("probe", {"x": 1.0, "y": 1.0, "z": -0.2, "success": bool(hit)}))
            self._comm._response_queue.put(("ok",))


def test_alignment_plunge_is_a_probe_and_contact_marks_the_hole_failed():
    conn = _HoleConn(touch=True)
    plugin = _plugin(conn)
    plugin._project.alignment_holes = [
        {"expected_x": 1.0, "expected_y": 1.0, "diameter": 2.0, "probed": False},
    ]
    plugin._alignment_probe_loop(conn)
    assert not any(c.startswith("G1 Z-") for c in conn.sent)  # never a blind plunge
    plunge = [c for c in conn.sent if c.startswith("G38.3 Z")]
    assert plunge and plunge[0].startswith("G38.3 Z-0.8000")
    assert not any(c.startswith("G38.3 X") for c in conn.sent)  # no wall probing at the wrong place
    # retracted after the contact
    assert conn.sent[conn.sent.index(plunge[0]) + 1].startswith("G1 Z5")
    hole = plugin._project.alignment_holes[0]
    assert hole["probed"] is False and hole["probe_error"] == "not_in_hole"
    msgs = plugin._plugin_manager.messages
    assert any(m["type"] == "alignment_hole_failed" and m["reason"] == "not_in_hole" for m in msgs)
    done = next(m for m in msgs if m["type"] == "alignment_complete")
    assert done["failed_holes"] == [{"index": 0, "reason": "not_in_hole"}]


def test_alignment_plunge_without_contact_goes_on_probing_the_walls():
    conn = _HoleConn(touch=False)
    plugin = _plugin(conn)
    plugin._project.alignment_holes = [
        {"expected_x": 1.0, "expected_y": 1.0, "diameter": 2.0, "probed": False},
    ]
    plugin._alignment_probe_loop(conn)
    assert any(c.startswith("G38.3 Z") for c in conn.sent)
    assert any(c.startswith("G38.3 X") for c in conn.sent)
    assert "probe_error" not in plugin._project.alignment_holes[0]


# ---------------------------------------------------------------- 5: stop during the drift check

class _DriftConn:
    _grbl_state = "Idle"
    _work_offset = {"x": 0.0, "y": 0.0, "z": 0.0}  # noqa: RUF012

    def __init__(self, plugin, press):
        self._comm = _ProbeComm()
        self.sent = []
        self.plugin = plugin
        self.press = press

    def commands(self, *cmds, internal=False):
        for c in cmds:
            self.sent.append((time.monotonic(), c))
            if c.startswith("G38.2"):
                if self.press == "stop":
                    self.plugin._probe_user_stop.set()
                    self.plugin._probe_stop.set()
                elif self.press == "reset":
                    self.plugin._probe_user_stop.set()
                    self.plugin._probe_reset.set()
                    self.plugin._probe_stop.set()
                # GRBL aborts the probe cycle without [PRB], only the banner comes
                self._comm._response_queue.put(("ok",))
                self._comm._response_queue.put(("reset",))
            else:
                self._comm._response_queue.put(("ok",))


def _drift_run(press):
    plugin = _plugin()
    hm = HeightMap()
    hm.generate_grid(0, 0, 20, 20, grid_x=2, grid_y=2, margin=0)
    for i in range(len(hm.points)):
        hm.set_probe_result(i, 0.0)
    plugin._height_map = hm
    conn = _DriftConn(plugin, press)
    plugin._get_grbl_connector = lambda: conn
    t0 = time.monotonic()
    plugin._autolevel_probe_loop(conn)
    return plugin, conn, time.monotonic() - t0


@pytest.mark.parametrize("press", ["stop", "reset", "grbl"])
def test_stop_or_reset_during_the_drift_check_ends_it_at_once(press):
    plugin, conn, took = _drift_run(press)
    assert took < 10
    types = [m["type"] for m in plugin._plugin_manager.messages]
    assert "autolevel_complete" not in types
    g38 = next(t for t, c in conn.sent if c.startswith("G38.2"))
    after = [c for t, c in conn.sent if t > g38]
    if press == "reset":
        assert after == []  # nothing moves after Reset
    if press == "grbl":
        # a reset the user did not press: GRBL restarted, nothing moves either
        assert after == []
        assert plugin._probe_reset.is_set()
        assert any(m["type"] == "autolevel_failed" and m.get("stage") == "drift_check"
                   for m in plugin._plugin_manager.messages)
    if press == "stop":
        assert all(not c.startswith("G0 X") for c in after)  # no travel after Stop


# ---------------------------------------------------------------- 6: silent link

def test_silent_grbl_is_halted_and_the_link_treated_as_lost():
    c, _received, sim, cleanup = tsp._make_setup(0.2)
    try:
        c.LINK_TIMEOUT = 1.5
        c.HOLD_TIMEOUT = 0.3
        rt = []
        orig_rt = sim.realtime

        def rec(byte):
            if byte in (0x21, 0x18, 0x7E):
                rt.append(byte)
            return orig_rt(byte)

        sim.realtime = rec
        orig_send = sim.send
        silent = {"on": False}
        sim.send = lambda line: None if silent["on"] else orig_send(line)
        gcode = "G21\nG90\nM3 S10000\nG1 Z-0.1 F100\n" + "\n".join(f"G1 X{i} F100" for i in range(1, 60))
        assert c.stream_gcode(gcode, "iso")
        time.sleep(1.0)
        silent["on"] = True
        assert tsp._wait_status(c, "failed", 15)
        fail = next(m for m in c._plugin_manager.messages if m.get("status") == "failed")
        assert fail["reason"] == "connection_lost"
        assert 0x21 in rt and 0x18 in rt  # hold and reset were tried
        assert c._comm is None
        assert c.state.name != "OPERATIONAL"
        assert any(m.get("type") == "connection_lost" for m in c._plugin_manager.messages)
    finally:
        cleanup()


# ---------------------------------------------------------------- 7: page load during a job

def test_snapshot_and_status_carry_the_job_state():
    c, _received, _sim, cleanup = tsp._make_setup(0.3)
    try:
        assert c.stream_gcode("G21\nG90\n" + "\n".join(f"G1 X{i % 5} F100" for i in range(50)), "t")
        time.sleep(0.5)
        snap = c.machine_snapshot()
        assert snap["streaming"] is True and snap["paused"] is False
        status = [m for m in c._plugin_manager.messages if m.get("type") == "status"]
        assert status and status[-1]["streaming"] is True
        c.stop_streaming(wait=True)
    finally:
        cleanup()


def test_frontend_adopts_a_running_job_after_a_page_load():
    body = JS[JS.index("self._adoptRunningJob = function"):]
    body = body[:body.index("self._applyMachineCheckReport")]
    assert "executionActive(true)" in body and "executionPaused(" in body
    assert "self._adoptRunningJob(snapshot.streaming, snapshot.paused)" in JS
    assert "self._adoptRunningJob(data.streaming, data.paused)" in JS
    startup = JS[JS.index("self.onStartup = function"):]
    startup = startup[:startup.index("self.onAfterBinding")]
    assert "self.requestStatus(true)" in startup
    # any non-final stream status means "running"
    progress = JS[JS.index('data.type === "stream_progress"'):]
    assert '["completed", "stopped", "failed"].indexOf(data.status) === -1' in progress[:900]
    assert "self.executionActive(true)" in progress[:900]
    align = JS[JS.index('data.type === "alignment_progress"'):]
    assert "self.alignmentProbing(true)" in align[:300]


# ---------------------------------------------------------------- suspected: probe-active

def test_probe_and_zero_counts_as_probing(app):
    c, received, _sim, cleanup = tsp._make_setup(0.0)
    try:
        c._probe_zero_running = True
        assert c.is_probing() and not c.accepts_manual_commands()
        c.commands("G0 X5")
        c.jog({"x": 1})
        assert c.stream_gcode("G1 X1 F100") is False
        time.sleep(0.3)
        assert received == []
        plugin = _plugin(c)
        assert plugin._is_probe_active()
        plugin._height_map = HeightMap()
        plugin._height_map.generate_grid(0, 0, 10, 10, grid_x=2, grid_y=2, margin=0)
        with app.app_context():
            _response, status = plugin._handle_start_autolevel({})
            assert status == 409
            _r, status = plugin.on_api_command("jog", {"axis": "X", "distance": 1})
            assert status == 409
    finally:
        cleanup()


def test_probe_and_zero_flag_is_cleared_when_it_ends():
    c, _received, _sim, cleanup = tsp._make_setup(0.0)
    try:
        c.probe("Z", -5.0, 100.0, zero=True, clearance=2.0)
        assert c.is_probe_zero_running() or True
        end = time.time() + 10
        while c.is_probe_zero_running() and time.time() < end:
            time.sleep(0.05)
        assert not c.is_probe_zero_running()
        assert c.accepts_manual_commands()
    finally:
        cleanup()


# ---------------------------------------------------------------- suspected: halt race

def test_no_new_job_or_probe_before_the_halt_has_finished(app):
    c, _received, _sim, cleanup = tsp._make_setup(0.3)
    try:
        assert c.stream_gcode("G21\nG90\n" + "\n".join(f"G1 X{i % 5} F600" for i in range(100)), "t")
        time.sleep(0.5)
        c.stop_streaming()  # the halt runs in the background
        assert c.halt_in_progress()
        end = time.time() + 5
        while c.is_streaming() and time.time() < end:
            time.sleep(0.02)
        assert c.stream_gcode("G1 X1 F600") is False  # the halt's reset would hit it
        plugin = _plugin(c)
        with app.app_context():
            _response, status = plugin._idle_block(c)
            assert status == 409
        end = time.time() + 10
        while c.halt_in_progress() and time.time() < end:
            time.sleep(0.05)
        assert not c.halt_in_progress()
        time.sleep(0.5)
        assert c.stream_gcode("G1 X1 F600") is True
    finally:
        cleanup()


def test_plugin_halt_flag_blocks_starting_a_probe_run(app):
    plugin = _plugin(type("C", (), {"_grbl_state": "Idle"})())
    plugin._halt_finished.clear()
    with app.app_context():
        _r, status = plugin._idle_block(plugin._get_grbl_connector())
    assert status == 409
    plugin._halt_finished.set()
    with app.app_context():
        assert plugin._idle_block(plugin._get_grbl_connector()) is None


# ---------------------------------------------------------------- suspected: WCS only from GRBL

def test_wcs_is_not_taken_from_a_refused_command():
    c, _received, sim, cleanup = tsp._make_setup(0.0)
    try:
        c._comm._response_callback = c._on_grbl_response
        sim.state = "Alarm"
        c.commands("G55")
        c.set_wcs("G56")
        time.sleep(0.4)
        assert c._current_wcs == "G54"  # GRBL refused both
        sim.state = "Idle"
        c.set_wcs("G56")
        end = time.time() + 3
        while c._current_wcs != "G56" and time.time() < end:
            time.sleep(0.05)
        assert c._current_wcs == "G56" and sim.wcs == "G56"
    finally:
        cleanup()


# ---------------------------------------------------------------- suspected: z_zeroed

@pytest.mark.parametrize("code, lost", [(1, True), (2, True), (3, True), (5, False), (9, False)])
def test_z_zeroed_is_dropped_when_an_alarm_loses_the_position(code, lost):
    c, _received, _sim, cleanup = tsp._make_setup(0.0)
    try:
        c._z_zeroed = True
        c._on_grbl_alarm(code, "x")
        assert c._z_zeroed is (not lost)
    finally:
        cleanup()


def test_z_zeroed_is_dropped_on_reset_and_reconnect_loss():
    c, _received, _sim, cleanup = tsp._make_setup(0.0)
    try:
        # A clean reset (feed hold first) keeps the position and the zero;
        # only the alarm of a reset during motion loses it
        c._z_zeroed = True
        c._on_grbl_reset("Grbl 1.1h")
        assert c._z_zeroed is True
        c._on_grbl_alarm(3, "Reset while in motion")
        assert c._z_zeroed is False
        c._z_zeroed = True
        c._on_connection_lost("cable")
        assert c._z_zeroed is False
    finally:
        cleanup()


# ---------------------------------------------------------------- suspected: probe timeout and oks

def test_probe_timeout_follows_travel_and_feed():
    t = GrblPcbPlugin()._probe_result_timeout
    assert t(5.0, 25.0) > t(5.0, 300.0)
    assert t(10.0, 25.0) > t(5.0, 25.0)
    assert t(5.0, 25.0) >= 5.0 / 25.0 * 60.0  # a slow probe is not cut off after 30 s
    assert t(5.0, 0.0) < float("inf")


def test_autolevel_waits_as_long_as_the_probe_travel_needs(monkeypatch):
    conn = _AutolevelConn()
    plugin = _plugin(conn)
    plugin._settings.V = dict(_S.V, probe_feed=20.0)  # 5 mm at 20 mm/min: 15 s
    hm = HeightMap()
    hm.generate_grid(0, 0, 10, 10, grid_x=2, grid_y=2, margin=0)
    plugin._height_map = hm
    seen = []
    orig = plugin._await_probe_result

    def spy(comm, offset, timeout=None):
        seen.append(timeout)
        return orig(comm, offset, timeout)

    monkeypatch.setattr(plugin, "_await_probe_result", spy)
    plugin._autolevel_probe_loop(conn)
    assert seen and all(t is not None and t > 30.0 for t in seen)


def test_failed_g38_3_leaves_no_stray_ok_in_the_queue():
    class Conn:
        _grbl_state = "Idle"
        _work_offset = {"x": 0.0, "y": 0.0, "z": 0.0}  # noqa: RUF012

        def __init__(self):
            self._comm = _ProbeComm()

        def commands(self, *commands, **kwargs):
            for command in commands:
                if command.startswith("G38.3"):
                    self._comm._response_queue.put(("probe", {"x": 0, "y": 0, "z": 0, "success": False}))
                self._comm._response_queue.put(("ok",))

    conn = Conn()
    plugin = _plugin(conn)
    assert plugin._probe_axis(conn._comm, conn, "X", 1.0, 25.0) is None
    assert conn._comm._response_queue.empty()


def test_probe_axis_stops_when_the_mode_words_are_refused():
    class Conn:
        _grbl_state = "Idle"
        _work_offset = {"x": 0.0, "y": 0.0, "z": 0.0}  # noqa: RUF012

        def __init__(self):
            self._comm = _ProbeComm()
            self.sent = []

        def commands(self, *commands, **kwargs):
            self.sent.extend(commands)
            self._comm._response_queue.put(("error", 9, "locked"))

    conn = Conn()
    plugin = _plugin(conn)
    assert plugin._probe_axis(conn._comm, conn, "X", 1.0, 25.0) is None
    assert plugin._probe_stop.is_set()


# ---------------------------------------------------------------- normal and tool-change jobs

def test_plain_job_and_tool_change_job_still_run_on_the_simulator():
    c, _received, sim, cleanup = tsp._make_setup(0.0)
    try:
        assert c.stream_gcode("G21\nG90\nG54\nM3 S9000\nG1 Z-0.1 F100\nG1 X5 Y5 F600\nM5\nM2\n", "plain")
        assert tsp._wait_status(c, "completed", 15)
        assert sim.mpos["x"] == pytest.approx(5.0)
    finally:
        cleanup()
    c, _received, sim, cleanup = tsp._make_setup(0.0)
    try:
        assert c.stream_gcode("G21\nG90\nG54\nG1 Z5 F200\nM5\nT2 M6\nM3 S9000\nG4 P0.1\nG0 X7 Y3\nM5\nM2\n", "tc")
        assert tsp._wait_status(c, "tool_change", 10)
        c.confirm_tool_change()
        assert tsp._wait_status(c, "completed", 20)
        assert sim.mpos["x"] == pytest.approx(7.0)
    finally:
        cleanup()


def test_status_message_reports_the_probe_input_and_the_ui_shows_it():
    c, _received, _sim, cleanup = tsp._make_setup(0.0)
    try:
        c._on_grbl_status({"state": "Idle", "pins": "P",
                           "mpos": {"x": 0.0, "y": 0.0, "z": 0.0}})
        status = [m for m in c._plugin_manager.messages if m.get("type") == "status"]
        assert status and status[-1]["pins"] == "P"
    finally:
        cleanup()
    assert 'self.probeTouching((data.pins || "").indexOf("P") !== -1)' in JS
    assert "visible: probeTouching" in (BASE / "templates" / "grblpcb_tab.jinja2").read_text()
