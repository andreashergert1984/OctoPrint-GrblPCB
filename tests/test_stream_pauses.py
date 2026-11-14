"""Streaming M6/M0 against the simulator over a pseudo terminal."""
import importlib.util
import os
import pathlib
import queue
import threading
import time
import tty

import pytest

from octoprint_grblpcb.connector import ConnectedGrblPrinter
from octoprint_grblpcb.grbl_comm import GrblComm

_spec = importlib.util.spec_from_file_location(
    "grbl_simulator", pathlib.Path(__file__).parent.parent / "tools" / "grbl_simulator.py"
)
grbl_simulator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grbl_simulator)


class _Listener:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _PluginManager:
    def __init__(self):
        self.messages = []

    def send_plugin_message(self, plugin, data):
        self.messages.append(data)


def _make_setup(time_scale):
    master, slave = os.openpty()
    tty.setraw(slave)
    received = []
    sim = grbl_simulator.GrblSimulator(lambda data: os.write(master, data), time_scale=time_scale)
    stop = threading.Event()
    lines = queue.Queue()

    def reader():
        # Like GRBL: bytes land in the RX buffer at once, the parser takes
        # lines when the planner has room
        buf = bytearray()
        while not stop.is_set():
            try:
                data = os.read(master, 1024)
            except OSError:
                return
            for byte in data:
                try:
                    is_realtime = sim.realtime(byte)
                except OSError:
                    return  # terminal closed
                if is_realtime:
                    continue
                if byte == 0x0A:
                    sim.received_bytes(len(buf) + 1)
                    lines.put(buf.decode())
                    buf.clear()
                else:
                    buf.append(byte)

    def parser():
        while not stop.is_set():
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            received.append(line)
            try:
                sim.line(line)
            except OSError:
                return  # terminal closed during cleanup

    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=parser, daemon=True).start()
    connector = ConnectedGrblPrinter(None, listener=_Listener(), port=os.ttyname(slave))
    connector._plugin_manager = _PluginManager()
    connector._comm = GrblComm(
        os.ttyname(slave),
        status_callback=connector._on_grbl_status,
        disconnect_callback=connector._on_connection_lost,
    )
    connector._comm.connect()
    threading.Thread(target=connector._poll_status_loop, daemon=True).start()

    def cleanup():
        connector._stop_polling.set()
        stop.set()
        if connector._comm is not None:
            connector._comm.disconnect()
        for fd in (master, slave):
            try:
                os.close(fd)
            except OSError:
                pass  # a test closed it to simulate a pulled cable

    connector._test_master_fd = master
    return connector, received, sim, cleanup


@pytest.fixture
def setup():
    connector, received, _sim, cleanup = _make_setup(0.0)
    yield connector, received
    cleanup()


def _wait_status(connector, status, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(m.get("status") == status for m in connector._plugin_manager.messages):
            return True
        time.sleep(0.02)
    return False


@pytest.mark.parametrize("command, status", [("M6 T2", "tool_change"), ("M0", "pause")])
def test_stream_pauses_safely_and_resumes(setup, command, status):
    connector, received = setup
    gcode = f"G21\nG90\nM3 S10000\nG1 Z-0.1 F100\nG1 X1 F600\n{command}\nM3 S10000\nG1 X2 F600\n"
    assert connector.stream_gcode(gcode, operation_name="test")
    assert _wait_status(connector, status)

    # Before waiting: absolute mode, tool up, spindle off; the pause
    # command itself never goes to GRBL (GRBL rejects M6)
    before = received[received.index("G1 X1 F600") + 1:]
    assert before[:3] == ["G90", "G1 Z5.000 F120", "M5"]
    assert not any(line.startswith(command.split()[0]) for line in received)
    assert "G1 X2 F600" not in received

    connector.confirm_tool_change()
    assert _wait_status(connector, "completed")
    assert received[-1] == "G1 X2 F600"


def test_manual_commands_during_pause_do_not_overflow_grbl():
    # Real-time motion so the planner fills and the RX buffer matters
    connector, received, sim, cleanup = _make_setup(1.0)
    try:
        moves = "\n".join(f"G1 X{i % 7}.5 F6000" for i in range(60))
        assert connector.stream_gcode(f"G21\nG90\nM0\n{moves}\n", operation_name="test")
        assert _wait_status(connector, "pause")
        # What a user does at a tool change: jog Z, probe, zero
        connector.commands("G91", "G0 Z1", "G0 Z-1", "G90", "G10 L20 P0 Z0")
        time.sleep(0.5)
        connector.confirm_tool_change()
        assert _wait_status(connector, "completed", timeout=30)
        assert not sim.rx_overflow
        assert received[-1] == "G1 X3.5 F6000"
    finally:
        cleanup()


def test_alarm_during_pause_requires_new_confirmation(setup):
    connector, received = setup
    assert connector.stream_gcode("G21\nG90\nM0\nG1 X1 F600\n", operation_name="test")
    assert _wait_status(connector, "pause")
    assert connector.accepts_manual_commands()

    # Probe upwards: no contact, ALARM:5; the user confirms anyway
    connector.commands("G91", "G38.2 Z1 F100", "G90")
    time.sleep(0.5)
    connector.confirm_tool_change()
    assert _wait_status(connector, "confirm_again")
    assert "G1 X1 F600" not in received

    connector.commands("$X")
    time.sleep(0.5)
    connector.confirm_tool_change()
    assert _wait_status(connector, "completed")
    assert received[-1] == "G1 X1 F600"
    assert not connector.accepts_manual_commands() or not connector.is_streaming()


@pytest.mark.parametrize("line, kind", [
    ("T2 M6", "tool_change"), ("M06", "tool_change"), ("M6", "tool_change"),
    ("M0", "pause"), ("M00", "pause"), ("M05", None), ("M5", None), ("G1 X6 F600", None),
])
def test_pause_kind(line, kind):
    assert ConnectedGrblPrinter._pause_kind(line) == kind


def test_tool_change_note_reaches_the_ui(setup):
    connector, received = setup
    gcode = "G21\nG1 Z5 F100\nM5\nT2 M6 ; drill 1.00 mm\nM3 S1000\nG1 X1 F600\n"
    assert connector.stream_gcode(gcode, operation_name="test")
    assert _wait_status(connector, "tool_change")
    msg = [m for m in connector._plugin_manager.messages if m.get("status") == "tool_change"][0]
    assert msg["command"] == "T2 M6" and msg["note"] == "drill 1.00 mm"
    assert not any("M6" in line for line in received)
    connector.confirm_tool_change()
    assert _wait_status(connector, "completed")


def _last(connector, status):
    return [m for m in connector._plugin_manager.messages if m.get("status") == status][-1]


def test_grbl_error_fails_the_job_and_halts_the_machine(setup):
    connector, received = setup
    # The rejected line could be a retract: nothing after it may run
    tail = "\n".join(f"G1 X{i} F600" for i in range(2, 40))
    assert connector.stream_gcode(f"G21\nG90\nG1 X1 F600\nG99\n{tail}\n", operation_name="test")
    assert _wait_status(connector, "failed")
    msg = _last(connector, "failed")
    assert msg["reason"] == "grbl_error" and msg["error_code"] == 20
    assert msg["failed_line"] == 4 and msg["failed_command"] == "G99"
    assert not any(m.get("status") == "completed" for m in connector._plugin_manager.messages)
    assert "G1 X39 F600" not in received
    assert not connector.is_streaming() or _wait_not_streaming(connector)


def _wait_not_streaming(connector, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and connector.is_streaming():
        time.sleep(0.02)
    return not connector.is_streaming()


def test_alarm_fails_the_job_instead_of_completing(setup):
    connector, received = setup
    # Probe upwards: no contact, ALARM:5
    assert connector.stream_gcode("G21\nG91\nG38.2 Z1 F100\nG90\nG1 X5 F600\n", operation_name="test")
    assert _wait_status(connector, "failed")
    assert _last(connector, "failed")["reason"] == "alarm"
    assert not any(m.get("status") == "completed" for m in connector._plugin_manager.messages)


def test_completed_only_when_the_machine_is_idle():
    connector, received, sim, cleanup = _make_setup(1.0)
    try:
        states = []
        original = connector._send_stream_progress

        def record(status, *args, **kwargs):
            if status == "completed":
                states.append(sim.state)
            original(status, *args, **kwargs)

        connector._send_stream_progress = record
        # 10 mm at 1200 mm/min: half a second of motion after the last ok
        assert connector.stream_gcode("G21\nG90\nG1 X10 F1200\n", operation_name="test")
        assert _wait_status(connector, "completed", timeout=10)
        assert states == ["Idle"]
        msg = _last(connector, "completed")
        assert msg["percent"] == 100 and msg["current_line"] == 3
    finally:
        cleanup()


def test_stop_during_motion_keeps_the_position():
    connector, received, sim, cleanup = _make_setup(1.0)
    try:
        assert connector.stream_gcode("G21\nG90\nG1 X100 F600\nG1 X0 F600\n", operation_name="test")
        deadline = time.time() + 5
        while sim.state != "Run" and time.time() < deadline:
            time.sleep(0.02)
        assert sim.state == "Run"
        connector.stop_streaming()
        assert _wait_status(connector, "stopped")
        # Reset only after the feed hold completed: no ALARM:3
        deadline = time.time() + 2
        while sim.state != "Idle" and time.time() < deadline:
            time.sleep(0.02)
        assert sim.state == "Idle"
        assert not any(m.get("type") == "alarm" for m in connector._plugin_manager.messages)
        assert _last(connector, "stopped")["current_line"] > 0
    finally:
        cleanup()


def test_lost_connection_fails_the_job():
    connector, received, sim, cleanup = _make_setup(1.0)
    try:
        assert connector.stream_gcode("G21\nG90\nG1 X100 F600\nG1 X0 F600\n", operation_name="test")
        assert _wait_status(connector, "progress") or _wait_status(connector, "started")
        os.close(connector._test_master_fd)  # cable pulled
        assert _wait_status(connector, "failed")
        assert _last(connector, "failed")["reason"] == "connection_lost"
        assert any(m.get("type") == "connection_lost" for m in connector._plugin_manager.messages)
        assert connector._comm is None
    finally:
        cleanup()


def test_connect_fails_when_nothing_answers(monkeypatch):
    master, slave = os.openpty()
    tty.setraw(slave)
    connector = ConnectedGrblPrinter(None, listener=_Listener(), port=os.ttyname(slave))
    monkeypatch.setattr(ConnectedGrblPrinter, "WAKE_UP_DELAY", 0.0)
    monkeypatch.setattr(ConnectedGrblPrinter, "FIRST_STATUS_TIMEOUT", 0.3)
    try:
        with pytest.raises(ConnectionError):
            connector.connect()
        assert connector._comm is None  # port closed again
    finally:
        os.close(master)
        os.close(slave)
