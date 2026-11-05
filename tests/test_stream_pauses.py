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
                if sim.realtime(byte):
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
        os.ttyname(slave), status_callback=connector._on_grbl_status
    )
    connector._comm.connect()
    threading.Thread(target=connector._poll_status_loop, daemon=True).start()

    def cleanup():
        connector._stop_polling.set()
        stop.set()
        connector._comm.disconnect()
        os.close(master)
        os.close(slave)

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
