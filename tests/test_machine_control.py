"""Regression tests for the machine-control review: reset, modal state,
WCS, probing lock-outs, stop buttons, RX byte counting."""
import logging
import queue
import threading
import time

import pytest
import test_stream_pauses as tsp
from flask import Flask
from flask_babel import Babel

from octoprint_grblpcb import GrblPcbPlugin
from octoprint_grblpcb.connector import ConnectedGrblPrinter
from octoprint_grblpcb.grbl_comm import GrblComm
from octoprint_grblpcb.pcb_project import PCBProject


@pytest.fixture
def app():
    app = Flask(__name__)
    Babel(app)
    return app


def _last_progress(connector):
    return [m for m in connector._plugin_manager.messages if m.get("type") == "stream_progress"][-1]


def _moves(n=200):
    return "\n".join(f"G1 X{i % 7}.5 F600" for i in range(n))


# ---------------------------------------------------------------- item 1

def test_simulator_flushes_rx_on_ctrl_x():
    _connector, _received, sim, cleanup = tsp._make_setup(0.3)
    try:
        flushed = []
        original = sim.on_reset
        sim.on_reset = lambda: (flushed.append(True), original())
        sim.realtime(0x18)
        assert flushed
    finally:
        cleanup()


def test_comm_reports_banner_as_reset_while_capturing():
    comm = GrblComm("/dev/null")
    resets = []
    comm._reset_callback = resets.append
    comm.begin_capture()
    comm._process_line("Grbl 1.1h ['$' for help]")
    assert comm._response_queue.get_nowait() == ("reset",)
    assert resets and comm.reset_count == 1


def test_reset_button_during_job_ends_the_stream():
    connector, received, _sim, cleanup = tsp._make_setup(0.3)
    try:
        assert connector.stream_gcode(f"G21\nG90\nM3 S1000\n{_moves()}\n", operation_name="t")
        time.sleep(1.5)
        connector.hold()
        time.sleep(0.5)
        connector.emergency_stop()
        time.sleep(1.0)
        n = len(received)
        time.sleep(2.0)
        assert not connector.is_streaming()
        assert len(received) == n
        assert _last_progress(connector)["status"] == "stopped"
    finally:
        cleanup()


def test_cancel_print_ends_the_stream():
    connector, received, _sim, cleanup = tsp._make_setup(0.3)
    try:
        assert connector.stream_gcode(f"G21\nG90\n{_moves()}\n", operation_name="t")
        time.sleep(1.0)
        connector.cancel_print()
        time.sleep(1.0)
        n = len(received)
        time.sleep(1.5)
        assert not connector.is_streaming()
        assert len(received) == n
    finally:
        cleanup()


def test_reset_from_the_board_fails_the_job():
    connector, _received, _sim, cleanup = tsp._make_setup(0.3)
    try:
        assert connector.stream_gcode(f"G21\nG90\n{_moves()}\n", operation_name="t")
        time.sleep(1.0)
        connector.hold()
        time.sleep(0.5)  # hold completed: the reset raises no ALARM:3
        connector._comm.soft_reset()  # not through the connector: a reset button
        deadline = time.time() + 5
        while connector.is_streaming() and time.time() < deadline:
            time.sleep(0.05)
        assert not connector.is_streaming()
        last = _last_progress(connector)
        assert last["status"] == "failed" and last["reason"] == "reset"
    finally:
        cleanup()


# ---------------------------------------------------------------- item 2

def test_modal_state_is_restored_after_tool_change():
    connector, received, sim, cleanup = tsp._make_setup(0.0)
    try:
        gcode = "G21\nG90\nG0 X10 Y10\nM3 S1000\nG1 X11 F450\nM5\nT2 M6\nG1 X20\n"
        assert connector.stream_gcode(gcode, operation_name="t")
        assert tsp._wait_status(connector, "tool_change")
        # what a user does: inches, relative, other feed
        connector.commands("G20", "G91", "F99")
        time.sleep(0.3)
        connector.confirm_tool_change()
        assert tsp._wait_status(connector, "completed", timeout=10)
        i = received.index("G1 X20")
        assert received[i - 1] == "G21 G90 G54 F450 S1000"
        assert sim.absolute and not sim.inches and sim.feed == 450
        assert sim.mpos["x"] == 20.0
    finally:
        cleanup()


def test_spindle_restarts_after_m0_with_startup_delay():
    connector, received, sim, cleanup = tsp._make_setup(0.0)
    try:
        assert connector.stream_gcode("G21\nG90\nM3 S8000\nG1 X1 F300\nM0\nG1 X2\n", operation_name="t")
        assert tsp._wait_status(connector, "pause")
        assert sim.spindle_mode == "M5"  # stopped for the pause
        connector.confirm_tool_change()
        assert tsp._wait_status(connector, "completed", timeout=10)
        i = received.index("G1 X2")
        assert received[i - 3:i] == ["G21 G90 G54 F300 S8000", "M3 S8000", "G4 P2.0"]
        assert sim.spindle_mode == "M3"
    finally:
        cleanup()


# ---------------------------------------------------------------- item 3

def test_job_runs_in_the_active_wcs_not_the_generated_one():
    connector, received, sim, cleanup = tsp._make_setup(0.0)
    try:
        connector.set_wcs("G55")
        connector.zero_axis("all")
        time.sleep(0.3)
        assert connector.stream_gcode("G21\nG54 ; work coordinate system\nG90\nG1 X5 F600\n", operation_name="t")
        assert tsp._wait_status(connector, "completed", timeout=10)
        assert "G54" not in received
        assert "G55" in received[received.index("$G"):]
        assert sim.wcs == "G55"
    finally:
        cleanup()


def test_active_wcs_is_read_back_from_grbl():
    connector, _received, sim, cleanup = tsp._make_setup(0.0)
    try:
        connector._comm.send_command("G56")  # behind the connector's back
        time.sleep(0.3)
        assert connector._current_wcs == "G54"
        assert connector.stream_gcode("G54\nG1 X5 F600\n", operation_name="t")
        assert tsp._wait_status(connector, "completed", timeout=10)
        assert sim.wcs == "G56" and connector._current_wcs == "G56"
    finally:
        cleanup()


def test_reset_puts_the_selected_wcs_back_to_g54():
    connector, _received, _sim, cleanup = tsp._make_setup(0.0)
    try:
        connector._comm._reset_callback = connector._on_grbl_reset
        connector.set_wcs("G57")
        time.sleep(0.2)
        connector._comm.soft_reset()
        deadline = time.time() + 3
        while connector._current_wcs != "G54" and time.time() < deadline:
            time.sleep(0.05)
        assert connector._current_wcs == "G54"
    finally:
        cleanup()


# ---------------------------------------------------------------- item 4

def test_manual_commands_are_rejected_while_probing():
    connector, received, _sim, cleanup = tsp._make_setup(0.0)
    try:
        connector._probe_active_check = lambda: True
        assert not connector.accepts_manual_commands()
        connector.commands("G0 X5")
        connector.jog({"x": 1})
        connector.zero_axis("all")
        connector.probe("Z")
        connector.home()
        connector.set_wcs("G55")
        connector.set_spindle(1000)
        time.sleep(0.3)
        assert received == []
        assert connector._current_wcs == "G54"
        # the probe run's own commands go through
        connector.commands("G0 X5", internal=True)
        time.sleep(0.3)
        assert received == ["G0 X5"]
    finally:
        cleanup()


class _Alive:
    def is_alive(self):
        return True


class _Stub:
    def __init__(self, state="Idle"):
        self._grbl_state = state
        self._work_offset = {"x": 0.0, "y": 0.0, "z": 0.0}
        self._z_zeroed = True
        self.halted = 0

    def is_streaming(self):
        return False

    def accepts_manual_commands(self):
        return True

    def _halt_machine(self):
        self.halted += 1


def _plugin(connector=None):
    plugin = GrblPcbPlugin()
    plugin._logger = logging.getLogger("test")
    plugin._save_project = lambda: None
    plugin._user_can = lambda permission: True
    plugin._project = PCBProject()
    plugin._get_grbl_connector = lambda: connector
    return plugin


@pytest.mark.parametrize("command, data", [
    ("jog", {"axis": "X", "distance": 1}),
    ("zero_axis", {"axis": "all"}),
    ("probe", {"axis": "Z"}),
    ("home", {}),
])
def test_manual_api_commands_409_while_probing(app, command, data):
    plugin = _plugin(_Stub())
    plugin._probe_thread = _Alive()
    with app.app_context():
        _, status = plugin.on_api_command(command, data)
    assert status == 409


# ---------------------------------------------------------------- item 5

@pytest.mark.parametrize("command", [
    "setup_autolevel", "new_project", "load_project", "rotate_project",
    "delete_project", "import_layer", "remove_layer",
])
def test_project_changes_409_while_probing(app, command):
    plugin = _plugin(_Stub())
    plugin._probe_thread = _Alive()
    project = plugin._project
    with app.app_context():
        _, status = plugin.on_api_command(command, {"project_id": "x", "angle": 90})
    assert status == 409
    assert plugin._project is project


def test_upload_409_while_probing(app):
    plugin = _plugin(_Stub())
    plugin._probe_thread = _Alive()
    with app.test_request_context("/upload", method="POST"):
        _, status = plugin.handle_upload()
    assert status == 409


# ---------------------------------------------------------------- item 6

@pytest.mark.parametrize("command", ["stop_autolevel", "stop_alignment_probe"])
def test_stop_probing_halts_the_machine_without_the_project_lock(app, command):
    stub = _Stub()
    plugin = _plugin(stub)
    plugin._probe_thread = _Alive()
    result = []
    with plugin._project_lock:  # a long running handler holds it
        def call():
            with app.app_context():
                result.append(plugin.on_api_command(command, {}))

        t = threading.Thread(target=call)
        t.start()
        t.join(timeout=3)
    assert not t.is_alive()
    assert plugin._halt_finished.wait(timeout=3)
    assert stub.halted == 1
    assert plugin._probe_stop.is_set() and plugin._probe_user_stop.is_set()


# ---------------------------------------------------------------- item 7

class _AlignConn:
    def __init__(self, comm, state="Idle", fail="g38"):
        self._comm = comm
        self._grbl_state = state
        self._work_offset = {"x": 0.0, "y": 0.0, "z": 0.0}
        self.sent = []
        self.fail = fail

    def commands(self, *commands, **kwargs):
        for c in commands:
            self.sent.append(c)
            if c.startswith("G38"):
                if self.fail == "exception":
                    raise RuntimeError("boom")
                self._comm._response_queue.put(("probe", {"x": 0, "y": 0, "z": 0, "success": False}))
            self._comm._response_queue.put(("ok",))


class _Comm:
    _stream_mode = False

    def __init__(self):
        self._response_queue = queue.Queue()


class _Settings:
    def get_float(self, path):
        return 1.0

    def get_int(self, path):
        return 1


def _alignment_run(state, fail):
    plugin = _plugin()
    plugin._settings = _Settings()
    plugin._project.alignment_holes = [
        {"expected_x": 1.0, "expected_y": 1.0, "diameter": 2.0, "probed": False},
        {"expected_x": 5.0, "expected_y": 1.0, "diameter": 2.0, "probed": False},
    ]
    conn = _AlignConn(_Comm(), state=state, fail=fail)
    plugin._plugin_manager = None
    plugin._alignment_probe_loop(conn)
    return conn


@pytest.mark.parametrize("fail", ["g38", "exception"])
def test_alignment_always_retracts_when_leaving_a_hole(fail):
    conn = _alignment_run("Idle", fail)
    assert conn.sent[-1].startswith("G1 Z1.0")
    # the hole plunge is a G38.3 Z; the failing probe is the first one sideways
    first_fail = next(
        i for i, c in enumerate(conn.sent)
        if c.startswith("G38") and (fail == "exception" or " Z" not in c)
    )
    assert conn.sent[first_fail + 1].startswith("G1 Z1.0")


def test_alignment_does_not_move_in_alarm():
    conn = _alignment_run("Alarm", "g38")
    assert conn.sent[-1].startswith("G38")  # nothing sent after the failure


# ---------------------------------------------------------------- item 8

def test_unexpected_error_in_the_stream_loop_halts_the_machine():
    connector, _received, _sim, cleanup = tsp._make_setup(0.0)
    try:
        halted = []
        connector._halt_machine = lambda: halted.append(True)
        original = connector._send_counted

        def broken(line, index):
            if line == "G1 X3 F600":
                raise RuntimeError("boom")
            return original(line, index)

        connector._send_counted = broken
        assert connector.stream_gcode("G21\nG90\nG1 X3 F600\n", operation_name="t")
        assert tsp._wait_status(connector, "failed")
        assert halted
        assert _last_progress(connector)["reason"] == "internal_error"
    finally:
        cleanup()


def test_lost_link_does_not_try_to_halt():
    connector, _received, _sim, cleanup = tsp._make_setup(0.0)
    try:
        halted = []
        connector._halt_machine = lambda: halted.append(True)

        def broken(line, index):
            if line == "G1 X3 F600":
                connector._link_lost = True
                raise OSError("gone")

        original = connector._send_counted
        connector._send_counted = lambda line, index: broken(line, index) or original(line, index)
        assert connector.stream_gcode("G21\nG90\nG1 X3 F600\n", operation_name="t")
        assert tsp._wait_status(connector, "failed")
        assert not halted
    finally:
        cleanup()


# ---------------------------------------------------------------- item 9/10

def test_job_on_hold_capability_is_not_claimed():
    assert ConnectedGrblPrinter.printer_capabilities.job_on_hold is False


def test_octoprint_pause_and_resume_act_on_the_stream():
    connector, received, _sim, cleanup = tsp._make_setup(0.3)
    try:
        assert connector.stream_gcode(f"G21\nG90\n{_moves(100)}\n", operation_name="t")
        time.sleep(1.0)
        connector.pause_print()
        assert connector._stream_pause.is_set()
        time.sleep(0.8)
        n = len(received)
        time.sleep(1.0)
        assert len(received) == n  # no new lines while paused
        connector.resume_print()
        assert not connector._stream_pause.is_set()
        time.sleep(1.0)
        assert len(received) > n
        connector.cancel_print()
    finally:
        cleanup()


# ---------------------------------------------------------------- item 11

@pytest.mark.parametrize("state", ["Run", "Jog", "Hold:0", "Home"])
def test_start_autolevel_and_send_to_cnc_require_idle(app, state):
    from octoprint_grblpcb.autolevel import HeightMap
    from octoprint_grblpcb.pcb_project import MillingOperation

    stub = _Stub(state)
    plugin = _plugin(stub)
    plugin._height_map = HeightMap()
    plugin._project.operations = [MillingOperation(
        name="Run 1", operation_type="isolation", layer_type="top_copper", gcode="G0 X0 Y0")]
    with app.app_context():
        _, status = plugin._handle_start_autolevel({})
        assert status == 409
        _, status = plugin._handle_send_to_cnc({"operation_index": 0, "force": True})
        assert status == 409


def test_stream_gcode_refuses_a_moving_machine():
    connector, received, _sim, cleanup = tsp._make_setup(0.3)
    try:
        connector._grbl_state = "Run"
        assert not connector.stream_gcode("G21\nG1 X1 F100\n", operation_name="t")
        assert received == []
    finally:
        cleanup()


# ---------------------------------------------------------------- item 12

def test_rx_buffer_counts_bytes_not_characters():
    class Comm:
        def __init__(self):
            self._response_queue = queue.Queue()
            self.sent = []

        def send_command(self, line):
            self.sent.append(line)

    connector = ConnectedGrblPrinter(None, listener=tsp._Listener(), port="x")
    connector._comm = Comm()
    line = "G1 X1 (Fräse äöü)"
    connector._send_counted(line, 0)
    assert connector._stream_pending == [(0, len(line.encode("utf-8")) + 1)]
    assert connector._stream_pending[0][1] > len(line) + 1


# ---------------------------------------------------------------- item 13

def _geometry_plugin():
    from shapely.geometry import box

    from octoprint_grblpcb.pcb_project import LayerType

    plugin = _plugin()
    plugin._settings = type("S", (), {
        "get_float": lambda self, path: {"tool_diameter": 0.1, "tool_angle": 60, "cut_depth": 0.1}.get(path[0], 1.0),
        "get_int": lambda self, path: 1,
        "get_boolean": lambda self, path: False,
        "get": lambda self, path: "G54",
    })()
    p = plugin._project
    p.board_min_x, p.board_min_y, p.board_width, p.board_height = 0.0, 0.0, 50.0, 30.0
    p.flip_axis = "X"
    plugin._geometries = {LayerType.BOTTOM_COPPER: [box(9, 9, 11, 11)], LayerType.TOP_COPPER: [box(9, 9, 11, 11)]}
    return plugin


def test_bottom_isolation_is_always_mirrored(app):
    plugin = _geometry_plugin()
    with app.app_context():
        plugin._handle_generate_isolation({"layer_type": "bottom_copper"})
        plugin._handle_generate_isolation({"layer_type": "top_copper"})
    ops = {o.layer_type: o for o in plugin._project.operations}
    assert ops["bottom_copper"].mirrored
    assert not ops["top_copper"].mirrored


def test_send_to_cnc_refuses_unmirrored_bottom_operation(app):
    from octoprint_grblpcb.pcb_project import MillingOperation

    plugin = _plugin(_Stub())
    plugin._project.operations = [MillingOperation(
        name="Isolation - bottom_copper", operation_type="isolation",
        layer_type="bottom_copper", gcode="G0 X1", mirrored=False)]
    with app.app_context():
        response, status = plugin._handle_send_to_cnc({"operation_index": 0, "force": True})
    assert status == 409
    assert response.get_json()["code"] == "bottom_layer_not_mirrored"


# ---------------------------------------------------------------- item 14

def test_uncut_narrow_gaps_are_reported_as_warnings(app, monkeypatch):
    import octoprint_grblpcb as mod

    plugin = _geometry_plugin()
    monkeypatch.setattr(mod, "plan_narrow_gap_passes", lambda copper, settings: ([], [(0.05, (12.0, 7.0))]))
    with app.app_context():
        body = plugin._handle_generate_isolation({"layer_type": "top_copper"}).get_json()
    gap = [w for w in body["warnings"] if w["code"] == "gap_uncut"]
    assert gap == [{"code": "gap_uncut", "gap": 0.05, "x": 12.0, "y": 7.0, "tool": 0.1}]


# ---------------------------------------------------------------- item 15/16

def test_probe_with_zero_sets_zero_on_contact_and_lifts():
    connector, received, sim, cleanup = tsp._make_setup(0.0)
    try:
        connector.probe("Z", -5.0, 100.0, zero=True, clearance=2.0)
        deadline = time.time() + 10
        while "G90" not in received and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        assert "G10 L20 P0 Z0" in received
        assert received.index("G10 L20 P0 Z0") < received.index("G0 Z2")
        assert connector._z_zeroed
        assert sim.absolute
        assert sim.wpos()["z"] == pytest.approx(2.0)
    finally:
        cleanup()


def test_probe_with_zero_does_nothing_blindly_without_contact():
    connector, received, sim, cleanup = tsp._make_setup(0.0)
    try:
        connector.probe("Z", 5.0, 100.0, zero=True)  # upwards: no contact
        deadline = time.time() + 10
        while "G90" not in received and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        assert "G10 L20 P0 Z0" not in received
        assert not any(c.startswith("G0 Z2") for c in received)
        assert not connector._z_zeroed
        connector.unlock()  # leaves G91 behind in alarm: unlock puts it right
        time.sleep(0.4)
        assert sim.absolute
    finally:
        cleanup()


def test_probe_api_passes_zero_and_goto_zero_is_absolute(app):
    calls = []

    class Conn(_Stub):
        def probe(self, *args, **kwargs):
            calls.append(("probe", args, kwargs))

        def commands(self, *commands, **kwargs):
            calls.append(("commands", commands))

    plugin = _plugin(Conn())
    plugin._settings = _Settings()
    with app.app_context():
        plugin.on_api_command("probe", {"axis": "Z", "zero": True})
        plugin.on_api_command("goto_zero", {})
    assert calls[0][0] == "probe" and calls[0][2]["zero"] is True
    assert calls[1][1][0] == "G21 G90"


def test_manual_z_probe_reaches_below_the_safe_height(app):
    # From the safe height the probe must reach the board even with a
    # shorter tool after a tool change; a 5 mm travel from 5 mm is not enough
    calls = []

    class Conn(_Stub):
        def probe(self, *args, **kwargs):
            calls.append(args)

    plugin = _plugin(Conn())

    class S(_Settings):
        def get_float(self, path):
            return {"probe_depth": -5.0, "safe_z_height": 5.0}.get(path[0], 1.0)

    plugin._settings = S()
    with app.app_context():
        plugin.on_api_command("probe", {"axis": "Z", "zero": True})
        plugin.on_api_command("probe", {"axis": "Z"})
    assert all(args[1] <= -10.0 for args in calls)


# ---------------------------------------------------------------- review: probe-and-zero result

def _probe_results(connector):
    return [m for m in connector._plugin_manager.messages if m.get("type") == "probe_zero_result"]


def _wait_probe_result(connector, timeout=10):
    deadline = time.time() + timeout
    while not _probe_results(connector) and time.time() < deadline:
        time.sleep(0.05)
    return _probe_results(connector)


def test_probe_zero_timeout_scales_with_travel_and_feed():
    t = ConnectedGrblPrinter.probe_zero_timeout
    assert t(-10.0, 25.0) > t(-5.0, 25.0)
    assert t(-10.0, 25.0) > t(-10.0, 100.0)
    # 10 mm at 25 mm/min is 24 s of fast probing, plus the safety factor
    assert t(-10.0, 25.0) >= 24.0 * 1.5
    assert t(-5.0, 1000.0) >= ConnectedGrblPrinter.PROBE_ZERO_SLACK
    assert t(-5.0, 0.0) < float("inf")  # zero feed does not divide by zero


def test_probe_with_zero_reports_success_with_the_contact_z():
    connector, _received, _sim, cleanup = tsp._make_setup(0.0)
    try:
        connector.probe("Z", -5.0, 100.0, zero=True, clearance=2.0)
        results = _wait_probe_result(connector)
        assert results and results[0]["success"] is True
        assert isinstance(results[0]["z"], float)
    finally:
        cleanup()


def test_probe_with_zero_reports_no_contact():
    connector, _received, _sim, cleanup = tsp._make_setup(0.0)
    try:
        connector.probe("Z", 5.0, 100.0, zero=True)  # upwards: no contact
        results = _wait_probe_result(connector)
        assert results[0] == {"type": "probe_zero_result", "success": False, "reason": "no_contact", "axis": "Z"}
    finally:
        cleanup()


def test_probe_with_zero_timeout_is_logged_and_reported(caplog):
    connector, received, _sim, cleanup = tsp._make_setup(1.0)
    try:
        connector.probe_zero_timeout = lambda depth, feed: 0.4
        with caplog.at_level(logging.WARNING):
            connector.probe("Z", -50.0, 10.0, zero=True)
            results = _wait_probe_result(connector, timeout=5)
        assert results[0]["success"] is False and results[0]["reason"] == "timeout"
        assert "G10 L20 P0 Z0" not in received
        assert any("Probe and zero failed" in r.getMessage() for r in caplog.records)
    finally:
        cleanup()


# ---------------------------------------------------------------- review: retract after stop / reset

class _RetractConn:
    def __init__(self, state="Idle"):
        self._grbl_state = state
        self.sent = []

    def commands(self, *commands, **kwargs):
        self.sent.extend(commands)


def test_retract_after_stop_selects_wcs_and_absolute_mode_first():
    plugin = _plugin()
    plugin._probe_user_stop.set()
    plugin._probe_wcs = "G55"
    conn = _RetractConn()
    plugin._retract_after_probing(conn, None, 2.0, 120.0)
    assert conn.sent == ["G21 G90 G55", "G1 Z2.0 F120"]


def test_retract_without_stop_does_not_reselect_the_wcs():
    plugin = _plugin()
    conn = _RetractConn()
    plugin._retract_after_probing(conn, None, 2.0, 120.0)
    assert conn.sent == ["G1 Z2.0 F120"]


def test_no_motion_after_the_reset_button(app):
    stub = _Stub()
    emitted = []
    stub.emergency_stop = lambda: emitted.append("reset")
    plugin = _plugin(stub)
    plugin._probe_thread = _Alive()
    with app.app_context():
        plugin._handle_reset({})
    assert emitted == ["reset"]
    assert plugin._probe_reset.is_set() and plugin._probe_user_stop.is_set()
    assert plugin._halt_finished.is_set()
    conn = _RetractConn()
    plugin._retract_after_probing(conn, None, 2.0, 120.0)
    assert conn.sent == []


def test_retract_waits_for_the_halt_to_finish():
    plugin = _plugin()
    plugin._probe_user_stop.set()
    plugin._halt_finished.clear()
    conn = _RetractConn()
    threading.Timer(0.3, plugin._halt_finished.set).start()
    started = time.time()
    plugin._retract_after_probing(conn, None, 2.0, 120.0)
    assert time.time() - started >= 0.25
    assert conn.sent


def test_stop_probing_does_not_wait_for_the_halt(app):
    class Slow(_Stub):
        def _halt_machine(self):
            time.sleep(0.6)
            self.halted += 1

    stub = Slow()
    plugin = _plugin(stub)
    plugin._probe_thread = _Alive()
    started = time.time()
    with app.app_context():
        plugin.on_api_command("stop_autolevel", {})
    assert time.time() - started < 0.4
    assert not plugin._halt_finished.is_set()
    assert plugin._halt_finished.wait(timeout=3)
    assert stub.halted == 1


# ---------------------------------------------------------------- review: reset during a job is immediate

def test_reset_button_during_a_job_resets_at_once_and_does_not_block():
    connector, _received, _sim, cleanup = tsp._make_setup(0.3)
    try:
        assert connector.stream_gcode(f"G21\nG90\n{_moves()}\n", operation_name="t")
        time.sleep(1.0)
        holds = []
        resets = []
        comm = connector._comm
        comm.feed_hold = lambda: holds.append(True)
        original = comm.soft_reset
        comm.soft_reset = lambda: (resets.append(time.time()), original())
        started = time.time()
        connector.emergency_stop()
        assert time.time() - started < 0.5
        assert resets and not holds  # Ctrl-X straight away, no feed hold
        deadline = time.time() + 5
        while connector.is_streaming() and time.time() < deadline:
            time.sleep(0.05)
        assert not connector.is_streaming()
    finally:
        cleanup()


def test_stop_streaming_holds_then_resets_without_blocking_the_caller():
    connector, _received, _sim, cleanup = tsp._make_setup(0.3)
    try:
        assert connector.stream_gcode(f"G21\nG90\n{_moves()}\n", operation_name="t")
        time.sleep(1.0)
        order = []
        comm = connector._comm
        orig_hold, orig_reset = comm.feed_hold, comm.soft_reset
        comm.feed_hold = lambda: (order.append("hold"), orig_hold())
        comm.soft_reset = lambda: (order.append("reset"), orig_reset())
        connector.HOLD_TIMEOUT = 1.0
        started = time.time()
        connector.stop_streaming()
        assert time.time() - started < 0.3
        deadline = time.time() + 5
        while "reset" not in order and time.time() < deadline:
            time.sleep(0.05)
        assert order[0] == "hold" and order[-1] == "reset"
    finally:
        cleanup()
