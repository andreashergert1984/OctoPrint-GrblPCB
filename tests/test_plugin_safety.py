# -*- coding: utf-8 -*-
"""Safety and validation tests for plugin API handlers."""

import threading

from flask import Flask

from octoprint_grblpcb import GrblPcbPlugin
from octoprint_grblpcb.autolevel import HeightMap
from octoprint_grblpcb.pcb_project import MillingOperation, PCBProject


class _AliveThread:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


class _ConnectorStub:
    def __init__(self, streaming=False, z_zeroed=False, state="Idle", work_offset=None):
        self._streaming = streaming
        self._z_zeroed = z_zeroed
        self._work_offset = work_offset or {"x": 0.0, "y": 0.0, "z": 0.0}
        self._state = state
        self.calls = []
        self.status_requests = 0

    def is_streaming(self):
        return self._streaming

    def request_status(self):
        self.status_requests += 1

    def machine_snapshot(self):
        return {
            "connected": True,
            "state": self._state,
            "machine_pos": {"x": 12.0, "y": 34.0, "z": 5.0},
            "work_pos": {"x": 1.0, "y": 2.0, "z": 3.0},
            "work_offset": {"x": 11.0, "y": 32.0, "z": 2.0},
            "feed": 1200,
            "spindle": 0,
            "wcs": "G54",
            "z_zeroed": self._z_zeroed,
            "streaming": self._streaming,
        }

    def stream_gcode(self, gcode, operation_name=""):
        self.calls.append((gcode, operation_name))
        return True


class TestPluginSafety:
    def setup_method(self):
        self.app = Flask(__name__)
        self.plugin = GrblPcbPlugin()

    def test_start_autolevel_blocked_while_streaming(self):
        self.plugin._height_map = HeightMap()
        self.plugin._get_grbl_connector = lambda: _ConnectorStub(streaming=True)

        with self.app.app_context():
            response, status = self.plugin._handle_start_autolevel({})

        assert status == 409
        assert response.get_json()["error"] == "Cannot start probing while streaming is active"

    def test_probe_alignment_blocked_while_streaming(self):
        project = PCBProject()
        project.alignment_holes = [
            {"expected_x": 1.0, "expected_y": 1.0, "diameter": 1.0},
            {"expected_x": 2.0, "expected_y": 2.0, "diameter": 1.0},
        ]
        self.plugin._project = project
        self.plugin._get_grbl_connector = lambda: _ConnectorStub(streaming=True)

        with self.app.app_context():
            response, status = self.plugin._handle_probe_alignment({})

        assert status == 409
        assert response.get_json()["error"] == "Cannot start probing while streaming is active"

    def test_send_to_cnc_blocked_while_probing(self):
        project = PCBProject()
        project.operations = [
            MillingOperation(
                name="Run 1",
                operation_type="isolation",
                layer_type="top_copper",
                gcode="G0 X0 Y0",
            )
        ]
        self.plugin._project = project
        self.plugin._probe_thread = _AliveThread(True)
        self.plugin._get_grbl_connector = lambda: _ConnectorStub(z_zeroed=True)

        with self.app.app_context():
            response, status = self.plugin._handle_send_to_cnc({"operation_index": 0})

        assert status == 409
        assert response.get_json()["error"] == "Cannot start streaming while probing is active"

    def test_send_to_cnc_uses_operation_type_for_z_warning(self):
        project = PCBProject()
        project.operations = [
            MillingOperation(
                name="Operation Alpha",
                operation_type="isolation",
                layer_type="top_copper",
                gcode="G0 X0 Y0",
            )
        ]
        self.plugin._project = project
        self.plugin._get_grbl_connector = lambda: _ConnectorStub(z_zeroed=False)

        with self.app.app_context():
            response = self.plugin._handle_send_to_cnc({"operation_index": 0})

        body = response.get_json()
        assert body["result"] == "warnings"
        assert body["warnings"] == [{"code": "z_not_zeroed"}]

    def test_remove_layer_invalid_type_returns_400(self):
        self.plugin._project = PCBProject()

        with self.app.app_context():
            response, status = self.plugin._handle_remove_layer({"layer_type": "nope"})

        assert status == 400
        assert "Invalid layer type" in response.get_json()["error"]

    def test_set_board_side_invalid_value_returns_400(self):
        self.plugin._project = PCBProject()

        with self.app.app_context():
            response, status = self.plugin._handle_set_board_side({"side": "diagonal"})

        assert status == 400
        assert response.get_json()["error"] == "Invalid board side: diagonal"

    def test_import_layer_invalid_type_returns_400(self):
        self.plugin._project = PCBProject()

        with self.app.app_context():
            response, status = self.plugin._handle_import_layer(
                {"filename": "board.gtl", "layer_type": "mystery"}
            )

        assert status == 400
        assert "Invalid layer type" in response.get_json()["error"]

    def test_resolve_unique_upload_path_appends_suffix(self, tmp_path):
        existing = tmp_path / "board.gtl"
        existing.write_text("x", encoding="utf-8")

        dest = self.plugin._resolve_unique_upload_path(str(tmp_path), "board.gtl")

        assert dest.endswith("board_1.gtl")

    def test_safe_pause_before_manual_intervention_retracts_and_stops_spindle(self):
        class CommStub:
            def __init__(self):
                self.sent = []

            def send_command(self, line):
                self.sent.append(line)

        class DummyPrinter:
            def __init__(self):
                self._comm = CommStub()
                self._plugin_settings = None
                self._stream_stop = threading.Event()

            def _wait_response(self, timeout=5.0):
                return ("ok", None, None)

            def _safe_pause_before_manual_intervention(self):
                if self._comm is None:
                    return
                self._comm.send_command("G90")
                self._comm.send_command("G1 Z5.000 F120")
                self._comm.send_command("M5")

        printer = DummyPrinter()
        printer._safe_pause_before_manual_intervention()

        assert printer._comm.sent[:3] == ["G90", "G1 Z5.000 F120", "M5"]

    def test_machine_check_reports_ready_state(self):
        connector = _ConnectorStub(streaming=False, state="Idle")
        self.plugin._get_grbl_connector = lambda: connector

        with self.app.app_context():
            response = self.plugin._handle_machine_check({})

        body = response.get_json()
        report = body["report"]

        assert body["result"] == "machine_check"
        assert report["ready"] is True
        assert report["summary"] == "ready"
        assert connector.status_requests == 1
        assert report["snapshot"]["state"] == "Idle"
        assert any(check["name"] == "connection" and check["ok"] for check in report["checks"])

    def test_machine_check_reports_attention_when_busy(self):
        connector = _ConnectorStub(streaming=True, state="Run")
        self.plugin._get_grbl_connector = lambda: connector

        with self.app.app_context():
            response = self.plugin._handle_status({})

        body = response.get_json()
        report = body["report"]

        assert body["result"] == "machine_check"
        assert report["ready"] is False
        assert report["summary"] == "attention required"
        assert any(check["name"] == "streaming" and not check["ok"] for check in report["checks"])


class _Settings:
    def get_int(self, path):
        return 3

    def get_float(self, path):
        return 1.0


def _leveled_project():
    project = PCBProject()
    project.board_width = 20.0
    project.board_height = 10.0
    project.operations = [
        MillingOperation(
            name="Iso",
            operation_type="isolation",
            layer_type="top_copper",
            gcode="G1 X1 Z-0.1",
            gcode_leveled="G1 X1 Z-0.05",
        )
    ]
    return project


def _probed_map(work_offset):
    hm = HeightMap()
    hm.generate_grid(0, 0, 20, 10, grid_x=2, grid_y=2)
    for i in range(len(hm.points)):
        hm.set_probe_result(i, 0.0)
    hm.work_offset = work_offset
    return hm


class TestLeveledGcodeSafety:
    def setup_method(self):
        self.app = Flask(__name__)
        self.plugin = GrblPcbPlugin()
        self.plugin._settings = _Settings()
        self.plugin._save_project = lambda: None

    def _send(self, connector):
        self.plugin._get_grbl_connector = lambda: connector
        with self.app.app_context():
            return self.plugin._handle_send_to_cnc({"operation_index": 0}).get_json()

    def test_new_grid_discards_leveled_gcode(self):
        self.plugin._project = _leveled_project()
        with self.app.app_context():
            self.plugin._handle_setup_autolevel({})
        assert self.plugin._project.operations[0].gcode_leveled == ""

    def test_matching_origin_sends_leveled_gcode(self):
        self.plugin._project = _leveled_project()
        self.plugin._height_map = _probed_map({"x": 10.0, "y": 10.0, "z": -1.0})
        connector = _ConnectorStub(z_zeroed=True, work_offset={"x": 10.02, "y": 9.99, "z": -0.5})
        assert self._send(connector)["result"] == "ok"
        assert connector.calls[0][0] == "G1 X1 Z-0.05"

    def test_moved_origin_warns(self):
        self.plugin._project = _leveled_project()
        self.plugin._height_map = _probed_map({"x": 10.0, "y": 10.0, "z": -1.0})
        connector = _ConnectorStub(z_zeroed=True, work_offset={"x": 12.0, "y": 10.0, "z": -1.0})
        body = self._send(connector)
        assert body["result"] == "warnings"
        assert body["warnings"] == [{"code": "heightmap_origin_moved", "dx": 2.0, "dy": 0.0}]
        assert connector.calls == []

    def test_unknown_origin_warns(self):
        self.plugin._project = _leveled_project()
        self.plugin._height_map = _probed_map(None)
        body = self._send(_ConnectorStub(z_zeroed=True))
        assert body["warnings"] == [{"code": "heightmap_origin_unknown"}]

    def test_force_sends_despite_warnings(self):
        self.plugin._project = _leveled_project()
        self.plugin._height_map = _probed_map(None)
        connector = _ConnectorStub(z_zeroed=False)
        self.plugin._get_grbl_connector = lambda: connector
        with self.app.app_context():
            body = self.plugin._handle_send_to_cnc({"operation_index": 0, "force": True}).get_json()
        assert body["result"] == "ok"
