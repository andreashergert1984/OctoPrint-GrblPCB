from octoprint_grblpcb.connector import ConnectedGrblPrinter, ConnectedPrinterState
from octoprint_grblpcb.grbl_comm import GrblComm


def test_alarm_goes_to_alarm_callback_not_error_callback():
    errors, alarms = [], []
    comm = GrblComm(
        "/dev/null",
        error_callback=lambda *a: errors.append(a),
        alarm_callback=lambda *a: alarms.append(a),
    )
    comm._process_line("ALARM:3")
    assert errors == []
    assert alarms == [(3, comm_alarm_text(3))]


def comm_alarm_text(code):
    from octoprint_grblpcb.grbl_comm import GRBL_ALARMS

    return GRBL_ALARMS[code]


class _Listener:
    def __init__(self):
        self.logs, self.states = [], []

    def on_printer_logs(self, line):
        self.logs.append(line)

    def on_printer_state_changed(self, state, **kwargs):
        self.states.append(state)

    def on_printer_position_changed(self, *args, **kwargs):
        pass


class _PluginManager:
    def __init__(self):
        self.messages = []

    def send_plugin_message(self, plugin, data):
        self.messages.append(data)


def _connector():
    c = ConnectedGrblPrinter.__new__(ConnectedGrblPrinter)
    c._listener = _Listener()
    c._plugin_manager = _PluginManager()
    c._state = ConnectedPrinterState.OPERATIONAL
    c._grbl_state = "Idle"
    c._machine_position = {"x": 0.0, "y": 0.0, "z": 0.0}
    c._work_position = {"x": 0.0, "y": 0.0, "z": 0.0}
    c._work_offset = {"x": 0.0, "y": 0.0, "z": 0.0}
    c._feed_rate = c._spindle_speed = 0
    c._current_wcs = "G54"
    return c


def test_alarm_is_logged_as_alarm_and_reported():
    c = _connector()
    c._on_grbl_alarm(3, "Reset while in motion.")
    assert c._listener.logs == ["<<< ALARM:3 (Reset while in motion.)"]
    assert c._plugin_manager.messages == [
        {"type": "alarm", "code": 3, "message": "Reset while in motion."}
    ]


def test_state_text_shows_alarm_and_refreshes_on_change():
    c = _connector()
    assert c.get_state_string() == "Operational"
    c._on_grbl_status({"state": "Alarm", "mpos": {"x": 0.0, "y": 0.0, "z": 0.0}})
    assert c.get_state_string() == "Alarm (GRBL)"
    c._on_grbl_status({"state": "Idle", "mpos": {"x": 0.0, "y": 0.0, "z": 0.0}})
    assert c.get_state_string() == "Operational"
    assert len(c._listener.states) == 2


def test_probe_alarm_codes_follow_grbl_1_1():
    from octoprint_grblpcb.grbl_comm import GRBL_ALARMS

    assert "initial state" in GRBL_ALARMS[4]
    assert "did not contact" in GRBL_ALARMS[5]
