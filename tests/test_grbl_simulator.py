import importlib.util
import pathlib
import time

import pytest

_spec = importlib.util.spec_from_file_location(
    "grbl_simulator", pathlib.Path(__file__).parent.parent / "tools" / "grbl_simulator.py"
)
grbl_simulator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grbl_simulator)


@pytest.fixture
def sim():
    out = []
    s = grbl_simulator.GrblSimulator(lambda data: out.append(data.decode()), time_scale=0.0)
    s.out = out
    return s


def _lines(sim):
    text = "".join(sim.out)
    sim.out.clear()
    return [line for line in text.split("\r\n") if line]


def _idle(sim):
    sim._planner.join()
    time.sleep(0.01)


def test_status_is_realtime_without_ok(sim):
    assert sim.realtime(ord("?"))
    lines = _lines(sim)
    assert lines[0].startswith("<Idle|MPos:0.000,0.000,0.000|FS:0,0|WCO:")
    assert "ok" not in lines


def test_g10_sets_offset_and_does_not_move(sim):
    sim.line("G0 X10 Y5")
    _idle(sim)
    sim.line("G10 L20 P0 X0 Y0 Z0")
    assert sim.mpos == {"x": 10.0, "y": 5.0, "z": 0.0}
    assert sim.wpos() == {"x": 0.0, "y": 0.0, "z": 0.0}


def test_probe_reports_surface(sim):
    sim.line("G38.2 Z-5 F100")
    lines = _lines(sim)
    assert lines[0].startswith("[PRB:0.000,0.000,") and lines[0].endswith(":1]")
    assert lines[-1] == "ok"
    assert sim.mpos["z"] == pytest.approx(sim.surface_at(0, 0))


def test_probe_miss_raises_alarm(sim):
    sim.line("G38.2 Z-0.5 F100")
    lines = _lines(sim)
    assert lines[0].endswith(":0]")
    assert "ALARM:5" in lines
    assert sim.state == "Alarm"


def test_jog_does_not_change_modal_state(sim):
    sim.line("$J=G91 X1 F500")
    _idle(sim)
    assert sim.absolute
    assert sim.mpos["x"] == pytest.approx(1.0)


def test_modal_motion_and_unsupported_m6(sim):
    sim.line("G1 X5 F200")
    sim.line("X6 Y6")
    sim.line("M6 T1")
    _idle(sim)
    assert _lines(sim) == ["ok", "ok", "error:20"]
    assert sim.mpos["y"] == pytest.approx(6.0)


def test_soft_reset_keeps_work_offsets(sim):
    sim.line("G0 X10")
    _idle(sim)
    sim.line("G10 L20 P0 X0")
    sim.realtime(0x18)
    assert sim.offsets["G54"]["x"] == pytest.approx(10.0)


def test_status_reports_are_parsed_by_plugin(sim):
    from octoprint_grblpcb.grbl_comm import GrblComm

    seen = []
    comm = GrblComm("/dev/null", status_callback=seen.append)
    sim.line("G0 X10 Y5")
    _idle(sim)
    sim.line("G10 L20 P0 X0 Y0")
    for _ in range(3):
        sim.realtime(ord("?"))
    for line in _lines(sim):
        if line.startswith("<"):
            comm._parse_status(line)
    assert all(s["wco"] == {"x": 10.0, "y": 5.0, "z": 0.0} for s in seen)
    assert seen[-1]["mpos"] == {"x": 10.0, "y": 5.0, "z": 0.0}


def test_reset_during_motion_raises_alarm(sim):
    sim.state = "Run"
    sim.realtime(0x18)
    lines = _lines(sim)
    assert lines[0] == "ALARM:3"
    assert sim.state == "Alarm"
    sim.line("G0 X1")
    assert _lines(sim) == ["error:9"]


def _sim_with_hole():
    out = []
    s = grbl_simulator.GrblSimulator(
        lambda data: out.append(data.decode()), time_scale=0.0, holes=[(10.0, 20.0, 3.0)], probe_diameter=1.0
    )
    s.out = out
    return s


def test_wall_probe_inside_hole():
    s = _sim_with_hole()
    s.line("G0 X10.2 Y20")
    _idle(s)
    s.line("G1 Z-1.5 F100")  # below the surface (-1.0)
    _idle(s)
    s.out.clear()
    s.line("G38.3 X12 F50")
    lines = _lines(s)
    # hole radius 1.5 minus probe radius 0.5: wall at x = 11.0
    assert lines[0] == "[PRB:11.000,20.000,-1.500:1]"
    s.line("G38.3 X8 F50")
    assert _lines(s)[0] == "[PRB:9.000,20.000,-1.500:1]"


def test_wall_probe_without_wall_g38_3_no_alarm():
    s = _sim_with_hole()
    s.line("G0 X30 Y30")
    _idle(s)
    s.line("G1 Z-1.5 F100")
    _idle(s)
    s.out.clear()
    s.line("G38.3 X35 F50")
    lines = _lines(s)
    assert lines[0].endswith(":0]") and "ALARM:5" not in lines
    assert s.state != "Alarm"


def test_overrides_reported():
    s = _sim_with_hole()
    for byte in (0x91, 0x91, 0x93, 0x96, 0x9B):
        s.realtime(byte)
    s.realtime(ord("?"))
    assert "Ov:121,50,90" in _lines(s)[0]
    s.realtime(0x90)
    s.realtime(ord("?"))
    assert "Ov:100,50,90" in _lines(s)[0]


def test_soft_reset_keeps_machine_position(sim):
    sim.line("G0 X10 Y5")
    _idle(sim)
    sim.realtime(0x18)
    assert sim.mpos == {"x": 10.0, "y": 5.0, "z": 0.0}
