"""GrblComm against the simulator over a real pseudo terminal."""
import importlib.util
import os
import pathlib
import threading
import time
import tty

import pytest

from octoprint_grblpcb.grbl_comm import GrblComm

_spec = importlib.util.spec_from_file_location(
    "grbl_simulator", pathlib.Path(__file__).parent.parent / "tools" / "grbl_simulator.py"
)
grbl_simulator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grbl_simulator)


@pytest.fixture
def comm():
    master, slave = os.openpty()
    tty.setraw(slave)
    sim = grbl_simulator.GrblSimulator(lambda data: os.write(master, data), time_scale=0.0)
    stop = threading.Event()

    def pump():
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
                    sim.line(buf.decode())
                    buf.clear()
                else:
                    buf.append(byte)

    threading.Thread(target=pump, daemon=True).start()
    c = GrblComm(os.ttyname(slave))
    c.connect()
    yield c
    stop.set()
    c.disconnect()
    os.close(master)
    os.close(slave)


def test_responses_arrive_without_polling(comm):
    comm._stream_mode = True
    comm.send_command("G0 X1")
    assert comm._response_queue.get(timeout=1.0)[0] == "ok"


def test_streaming_is_not_limited_by_status_interval(comm):
    comm._stream_mode = True
    lines = 200
    start = time.monotonic()
    for i in range(lines):
        comm.send_command(f"G1 X{i % 10}.5 F1000")
        assert comm._response_queue.get(timeout=1.0)[0] == "ok"
    # One line per 0.25 s poll cycle would take 50 s
    assert time.monotonic() - start < 5.0


def test_disconnect_stops_reader(comm):
    reader = comm._reader_thread
    comm.disconnect()
    assert not reader.is_alive()
