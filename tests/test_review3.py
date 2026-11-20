"""Regression tests for the third acceptance review."""
import pathlib
import queue
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import octoprint.filemanager  # noqa: F401
import test_review2_machine as r2
import test_stream_pauses as tsp

from octoprint_grblpcb.autolevel import HeightMap


def test_probe_move_leaves_surplus_ok_from_refresh_wcs():
    """No $G for internal commands: its ok would be one answer too many in the capture queue."""
    c, received, _sim, cleanup = tsp._make_setup(0.0)
    try:
        plugin = r2._plugin(c)
        plugin._probe_wcs = "G54"
        comm = c._comm
        comm._stream_mode = True
        comm.drain_responses()
        plugin._probe_move(c, comm, "G1 Z2 F600", "G0 X1 Y1", "G1 Z1 F600")
        time.sleep(0.5)
        left = []
        while True:
            try:
                left.append(comm._response_queue.get_nowait())
            except queue.Empty:
                break
        print("sent:", received, "left:", left)
        assert "$G" not in received  # internal commands do not ask for the WCS
        assert left == [], f"surplus answers left in the queue: {left}"
    finally:
        cleanup()


class _Comm:
    def __init__(self):
        self._response_queue = queue.Queue()
        self._stream_mode = False


class _RealisticConn:
    """Like the real connector: a line naming a WCS also sends $G (ok); last move of a block is refused."""
    _grbl_state = "Idle"

    def __init__(self, refuse):
        self._comm = _Comm()
        self._work_offset = {"x": 0.0, "y": 0.0, "z": 0.0}
        self.sent = []
        self.refuse = refuse

    def commands(self, *commands, **kwargs):
        q = self._comm._response_queue
        for command in commands:
            self.sent.append(command)
            if command == self.refuse and not getattr(self, "_refused", False):
                self._refused = True
                q.put(("error", 15, "Travel exceeded"))
                continue
            if command.startswith("G38.2"):
                q.put(("probe", {"x": 0.0, "y": 0.0, "z": -0.1, "success": True}))
            q.put(("ok",))
            if "G54" in command and not kwargs.get("internal"):
                self.sent.append("$G")
                q.put(("ok",))


def test_refused_last_move_is_noticed_before_the_probe_is_sent():
    conn = _RealisticConn(refuse="G1 Z2.0 F600")
    plugin = r2._plugin(conn)
    plugin._probe_wcs = "G54"
    hm = HeightMap()
    hm.generate_grid(0, 0, 20, 20, grid_x=2, grid_y=2, margin=0)
    plugin._height_map = hm
    plugin._autolevel_probe_loop(conn)
    print(conn.sent)
    first_g38 = next((i for i, c in enumerate(conn.sent) if c.startswith("G38.2")), None)
    assert first_g38 is None, f"G38.2 sent although the move before it was refused: {conn.sent[:first_g38 + 1]}"


def test_manual_probe_reports_nothing_when_grbl_refuses_it():
    """GRBL in Jog answers error:9 to G-code: the probe thread waits its whole timeout."""
    c, received, sim, cleanup = tsp._make_setup(0.0)
    try:
        orig = sim._gcode

        def locked(line, jog):
            if not jog:
                raise tsp.grbl_simulator.GrblError(9)
            return orig(line, jog)
        sim._gcode = locked
        c.probe("Z", -10.0, 100.0)
        time.sleep(3.0)
        print("received", received, "running", c.is_probe_zero_running(), "timeout", c.probe_zero_timeout(-10, 100))
        assert not c.is_probe_zero_running(), "probe still 'running' 3 s after GRBL refused it"
    finally:
        cleanup()


def test_manual_probe_target_uses_a_fresh_position():
    c, received, _sim, cleanup = tsp._make_setup(0.0)
    try:
        c.commands("G21 G90", "G0 Z4")
        r2._idle(c)
        c.commands("G0 Z8")     # e.g. a jog/lift that just finished
        time.sleep(0.05)        # status poll is every 250 ms
        print("work z at probe start", c._work_position["z"])
        c.probe("Z", -10.0, 100.0)
        end = time.time() + 10
        while c.is_probe_zero_running() and time.time() < end:
            time.sleep(0.05)
        g38 = [line for line in received if line.startswith("G38.2")]
        print(g38)
        assert g38[0].startswith("G38.2 Z-2.000"), g38
    finally:
        cleanup()


def test_wcs_shown_after_a_job_matches_grbl_after_a_wcs_switch_in_a_pause():
    c, _received, sim, cleanup = tsp._make_setup(0.0)
    try:
        c._comm._response_callback = c._on_grbl_response
        gcode = "G21\nG90\nG54\nG1 Z1 F100\nM0\nG1 X2 F600\n"
        assert c.stream_gcode(gcode, "t")
        assert tsp._wait_status(c, "pause")
        c.set_wcs("G55")          # user switches WCS during the pause
        time.sleep(0.5)
        c.confirm_tool_change()
        assert tsp._wait_status(c, "completed", 10)
        time.sleep(0.3)
        print("connector wcs", c._current_wcs, "grbl wcs", sim.wcs)
        assert c._current_wcs == sim.wcs
    finally:
        cleanup()


def test_manual_probe_reports_rejection_when_grbl_refuses_it():
    c, _received, sim, cleanup = tsp._make_setup(0.0)
    try:
        messages = []
        c._plugin_manager = type("PM", (), {"send_plugin_message": lambda self, n, m: messages.append(m)})()
        orig = sim._gcode

        def locked(line, jog):
            if not jog:
                raise tsp.grbl_simulator.GrblError(9)
            return orig(line, jog)
        sim._gcode = locked
        c.probe("Z", -10.0, 100.0, zero=True)
        end = time.time() + 5
        while c.is_probe_zero_running() and time.time() < end:
            time.sleep(0.05)
        assert not c.is_probe_zero_running()
        assert any(m.get("reason") == "rejected" and m["success"] is False for m in messages), messages
    finally:
        cleanup()


def test_handle_probe_refuses_while_the_machine_is_not_idle():
    from flask import Flask
    from flask_babel import Babel
    app = Flask(__name__)
    Babel(app)
    conn = _RealisticConn(refuse=None)
    conn._grbl_state = "Run"
    plugin = r2._plugin(conn)
    plugin._require_grbl_connection = lambda: (conn, None)
    called = []
    conn.probe = lambda *a, **k: called.append(a)
    with app.test_request_context():
        resp = plugin._handle_probe({"axis": "Z"})
    assert resp[1] == 409
    assert called == []


def test_slow_probe_goes_only_a_little_past_the_contact():
    c, received, _sim, cleanup = tsp._make_setup(0.0)
    try:
        c.commands("G21 G90", "G0 Z4")
        r2._idle(c)
        c.probe("Z", -10.0, 100.0)
        end = time.time() + 15
        while c.is_probe_zero_running() and time.time() < end:
            time.sleep(0.05)
        g38 = [line for line in received if line.startswith("G38.2")]
        assert len(g38) == 2, g38
        fast = float(g38[0].split("Z")[1].split()[0])
        slow = float(g38[1].split("Z")[1].split()[0])
        assert fast == -6.0
        # contact at the simulator's surface; slow target = back-off - 1 mm further
        back = [line for line in received if line.startswith("G0 Z") and line != "G0 Z4"][-1]
        back_z = float(back.split("Z")[1])
        assert abs(slow - (back_z - 2 * c.PROBE_BACKOFF)) < 1e-6, (back, g38)
    finally:
        cleanup()


def test_wcs_restore_line_sets_the_tracked_wcs_without_a_status_round_trip():
    # the connector's own bookkeeping after the restore line
    import inspect

    from octoprint_grblpcb.connector import ConnectedGrblPrinter as GrblConnector
    src = inspect.getsource(GrblConnector._restore_modal_state)
    assert "self._current_wcs = self._job_wcs" in src


def test_drill_tool_change_lifts_before_the_spindle_starts():
    import re

    from octoprint_grblpcb.gerber_parser import DrillInfo
    from octoprint_grblpcb.toolpath import ToolpathSettings, generate_drill_gcode
    drills = [DrillInfo(tool_number=1, diameter=0.8, holes=[(1.0, 1.0)]),
              DrillInfo(tool_number=2, diameter=1.0, holes=[(2.0, 2.0)])]
    lines = [ln.split(";")[0].strip() for ln in generate_drill_gcode(drills, ToolpathSettings(cut_depth=1.6, safe_z=5)).splitlines()]
    lines = [ln for ln in lines if ln]
    for i, ln in enumerate(lines):
        if re.match(r"M3\b", ln):
            assert lines[i - 1].startswith(("G0 Z5", "G1 Z5")), lines[i - 3:i + 1]


def test_probe_depth_hint_is_not_stale_and_translated():
    tpl = (ROOT / "octoprint_grblpcb/templates/grblpcb_settings.jinja2").read_text()
    assert "negative for Z probing down" not in tpl
    po = (ROOT / "translations/de/LC_MESSAGES/messages.po").read_text()
    assert "\nmsgid \"Maximum probe travel (negative" not in po


def test_clear_button_and_dialog_title_agree_in_german():
    from babel.messages.pofile import read_po
    with (ROOT / "translations/de/LC_MESSAGES/messages.po").open("rb") as fh:
        cat = read_po(fh)
    assert cat.get("Clear").string == "Löschen"
    assert cat.get("Clear the alignment?").string == "Ausrichtung löschen?"


def test_startup_status_request_is_silent_and_pause_banner_is_cleared():
    js = (ROOT / "octoprint_grblpcb/static/js/grblpcb.js").read_text()
    assert "self.requestStatus(true);" in js
    assert '_sendCommand("status", {}, {silent: silent === true})' in js
    body = js[js.index("self._adoptRunningJob = function"):]
    body = body[:body.index("self._applyMachineCheckReport")]
    assert "_adoptedPauseBanner" in body and "A job is running" in body
