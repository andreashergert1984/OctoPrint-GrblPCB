# -*- coding: utf-8 -*-
"""
OctoPrint-GrblPCB

A plugin for OctoPrint that adds support for GRBL-based CNC machines,
specifically tailored for PCB isolation milling operations.

Features:
- GRBL connector - native OctoPrint connector for GRBL controllers
- PCB-specific workflow (probe, mill, drill)
- Work coordinate system (WCS) management
- Spindle control integration
- Tool change management
"""

import os
import queue
import threading
import time
import traceback

import octoprint.plugin
from octoprint.events import Events
from flask_babel import gettext
import re

from .pcb_project import (
    PCBProject,
    LayerType,
    BoardSide,
    ProjectState,
    MillingOperation,
    guess_layer_type,
    list_projects,
    delete_project,
)
from .gerber_parser import parse_gerber_file, parse_excellon_file, merge_geometries
from .toolpath import (
    ToolpathSettings,
    generate_isolation_gcode,
    generate_drill_gcode,
    generate_outline_gcode,
    generate_mask_expose_gcode,
    mirror_coordinates_x,
    mirror_coordinates_y,
    AlignmentTransform,
    compute_alignment_transform,
    apply_alignment_transform,
)
from .autolevel import HeightMap, apply_autolevel, height_map_origin_check


class GrblPcbPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.BlueprintPlugin,
):
    """
    Main plugin class for GRBL PCB milling support.
    
    This plugin registers a GRBL connector with OctoPrint to enable
    native serial communication with GRBL-based CNC controllers.
    """

    def __init__(self):
        super().__init__()
        self._grbl_connector = None
        self._project: PCBProject = None
        self._geometries: dict = {}  # LayerType -> list of Shapely geoms
        self._geometries_unrotated: dict = {}  # Original unrotated geometries
        self._drills_unrotated: list = []  # Original unrotated drill data
        self._height_map: HeightMap = None
        self._probe_thread: threading.Thread = None
        self._probe_stop = threading.Event()
        self._upload_dir = None
        # Lock protecting _project, _geometries, _height_map from concurrent
        # access between Flask request threads and background probe threads.
        self._project_lock = threading.Lock()

    # -------------------------------------------------------------------------
    # Plugin Initialization
    # -------------------------------------------------------------------------

    def initialize(self):
        """Register the GRBL connector with OctoPrint."""
        # Import connector to register it with the metaclass
        from .connector import ConnectedGrblPrinter
        
        # Inject dependencies
        ConnectedGrblPrinter._event_bus = self._event_bus
        ConnectedGrblPrinter._file_manager = self._file_manager
        ConnectedGrblPrinter._plugin_manager = self._plugin_manager
        ConnectedGrblPrinter._plugin_settings = self._settings
        
        # Create upload/project directory
        self._upload_dir = os.path.join(
            self.get_plugin_data_folder(), "pcb_projects"
        )
        os.makedirs(self._upload_dir, exist_ok=True)
        
        self._logger.info("GRBL connector registered")

    # -------------------------------------------------------------------------
    # StartupPlugin
    # -------------------------------------------------------------------------

    def on_after_startup(self):
        self._logger.info("GrblPCB plugin started!")
        self._logger.info("GRBL connector available for CNC connections")

        # Try to load the most recently modified project
        self._load_last_project()

    # -------------------------------------------------------------------------
    # Project Persistence Helpers
    # -------------------------------------------------------------------------

    def _save_project(self):
        """Save the current project to disk (auto-save).

        Caller MUST hold ``self._project_lock`` or be in a section where
        exclusive access to ``self._project`` is guaranteed (e.g. startup).
        """
        if self._project is None or self._upload_dir is None:
            return
        try:
            self._project.save(self._upload_dir)
        except (OSError, ValueError, TypeError) as e:
            self._logger.error(f"Failed to save project: {e}")

    def _load_last_project(self):
        """Load the most recently modified project on startup."""
        if not self._upload_dir or not os.path.isdir(self._upload_dir):
            return

        # Find project dirs sorted by modification time (newest first)
        candidates = []
        for entry in os.listdir(self._upload_dir):
            project_dir = os.path.join(self._upload_dir, entry)
            project_json = os.path.join(project_dir, "project.json")
            if os.path.isdir(project_dir) and os.path.isfile(project_json):
                mtime = os.path.getmtime(project_json)
                candidates.append((mtime, project_dir))

        if not candidates:
            return

        candidates.sort(reverse=True)
        latest_dir = candidates[0][1]

        try:
            self._project = PCBProject.load(latest_dir)
            # Re-parse geometries from layer files so toolpath generation works
            self._geometries = {}
            for lt, layer in self._project.layers.items():
                if os.path.isfile(layer.filepath):
                    try:
                        if lt == LayerType.DRILL:
                            _, drills = parse_excellon_file(layer.filepath)
                            self._project.drills = drills
                        else:
                            parsed_layer, geometries = parse_gerber_file(
                                layer.filepath
                            )
                            self._geometries[lt] = geometries
                            # Update layer bounds from re-parsed geometry
                            layer.min_x = parsed_layer.min_x
                            layer.min_y = parsed_layer.min_y
                            layer.max_x = parsed_layer.max_x
                            layer.max_y = parsed_layer.max_y
                            layer.width = parsed_layer.width
                            layer.height = parsed_layer.height
                    except Exception as e:
                        self._logger.warning(
                            f"Could not re-parse {layer.filepath}: {e}"
                        )

            # Recalculate board bounds from updated layers
            self._project._update_board_dimensions()

            # Snapshot unrotated state before applying stored rotation
            self._snapshot_unrotated()

            # Re-apply stored rotation to freshly parsed geometries
            if self._project.rotation != 0:
                self._apply_rotation_to_geometries(self._project.rotation)

            # Restore height map if present
            if self._project.height_map:
                self._height_map = HeightMap.from_dict(self._project.height_map)

            self._logger.info(
                f"Restored project: {self._project.name} ({self._project.id}), "
                f"{len(self._geometries)} layer geometries loaded"
                f", rotation={self._project.rotation}°"
            )
        except Exception as e:
            self._logger.warning(f"Could not load last project: {e}")

    def _get_project_upload_dir(self):
        """Return the upload directory for the current project.

        Files are stored in <base_dir>/<project_id>/ so each project
        keeps its own Gerber/drill source files alongside project.json.
        """
        if self._project is None:
            return self._upload_dir
        project_dir = self._project.get_project_dir(self._upload_dir)
        os.makedirs(project_dir, exist_ok=True)
        return project_dir

    # -------------------------------------------------------------------------
    # SettingsPlugin
    # -------------------------------------------------------------------------

    def get_settings_defaults(self):
        return dict(
            # PCB milling defaults
            default_feed_rate=500,  # mm/min
            default_jog_feed=1000,  # mm/min for jogging
            default_spindle_speed=10000,  # RPM
            safe_z_height=5.0,  # mm - safe height for rapid moves
            travel_z_height=2.0,  # mm - Z for rapid travel between cuts
            probe_depth=-5.0,  # mm - maximum probe depth
            probe_feed=25,  # mm/min - probing feed rate
            
            # Tool settings
            tool_diameter=0.1,  # mm - default V-bit tip diameter
            tool_angle=60,  # degrees - V-bit angle
            cut_depth=0.1,  # mm - isolation cut depth
            plunge_rate=100,  # mm/min - plunge feed rate
            z_max_feed=120,  # mm/min (2 mm/s) - max Z travel speed (replaces G0 Z with G1 Z)
            
            # Isolation milling
            isolation_passes=1,
            isolation_overlap=0.0,  # mm extra offset per pass
            isolation_max_width=0.0,  # mm total clearance width (0 = use fixed passes)
            isolation_clear_gaps=False,  # auto-detect and clear all gaps
            depth_per_pass=0.0,  # mm per Z-depth pass (0 = full depth in one pass)
            
            # Drilling
            drill_feed=100,  # mm/min
            peck_depth=0.5,  # mm per peck
            board_thickness=1.6,  # mm
            breakthrough_depth=0.2,  # mm below the board for drilling and outline
            
            # Outline
            outline_tab_width=2.0,  # mm
            outline_tab_height=0.5,  # mm
            outline_num_tabs=4,
            
            # UV Mask Exposure
            uv_led_offset_x=0.0,  # mm
            uv_led_offset_y=0.0,  # mm
            uv_spot_size=0.5,  # mm
            uv_expose_speed=200,  # mm/min
            uv_expose_z=5.0,  # mm
            uv_power=255,  # 0-255
            
            # Autolevel
            autolevel_grid_x=5,
            autolevel_grid_y=5,
            autolevel_margin=1.0,  # mm
            autolevel_max_segment=1.0,  # mm
            
            # Double-sided
            flip_axis="X",
            
            # Work coordinate system
            wcs_default="G54",  # Default work coordinate system
            
            # Spindle control
            spindle_min_rpm=0,
            spindle_max_rpm=24000,
            spindle_delay=2.0,  # seconds - delay after spindle start
            
            # Safety settings
            soft_limits=True,
            hard_limits=True,
            
            # UI settings
            show_machine_position=True,
            show_work_position=True,
            ui_help_mode="detailed",
        )

    def on_settings_save(self, data):
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        self._logger.info("GrblPCB settings saved")

    # -------------------------------------------------------------------------
    # TemplatePlugin
    # -------------------------------------------------------------------------

    def get_template_configs(self):
        return [
            # Connection options for GRBL connector
            {
                "type": "connection_options",
                "name": gettext("GRBL CNC Connection"),
                "connector": "grbl",
                "template": "grblpcb_connection_option.jinja2",
                "custom_bindings": True,
            },
            # Settings panel
            {
                "type": "settings",
                "name": gettext("GRBL CNC / PCB Milling"),
                "template": "grblpcb_settings.jinja2",
                "custom_bindings": False,
            },
            # Main CNC control tab
            {
                "type": "tab",
                "name": "CNC/PCB",
                "template": "grblpcb_tab.jinja2",
                "custom_bindings": True,
            },
        ]

    def get_template_vars(self):
        return dict(
            wcs_options=["G54", "G55", "G56", "G57", "G58", "G59"],
        )

    def is_template_autoescaped(self):
        return True

    # -------------------------------------------------------------------------
    # AssetPlugin
    # -------------------------------------------------------------------------

    def get_assets(self):
        return dict(
            js=["js/labels.js", "js/workspace_renderer.js", "js/grblpcb.js"],
            css=["css/grblpcb.css"],
        )

    # -------------------------------------------------------------------------
    # SimpleApiPlugin
    # -------------------------------------------------------------------------

    def is_api_protected(self):
        """API requires authentication."""
        return True

    def get_api_commands(self):
        return dict(
            # CNC control
            probe=["axis"],
            home=[],
            unlock=[],
            reset=[],
            hold=[],
            resume=[],
            set_wcs=["wcs"],
            zero_axis=["axis"],
            goto_zero=[],
            status=[],
            set_spindle=["speed"],
            spindle_on=[],
            spindle_off=[],
            jog=["axis", "distance", "feed"],
            # PCB project
            new_project=[],
            get_project=[],
            list_projects=[],
            load_project=["project_id"],
            delete_project=["project_id"],
            import_layer=["filename", "layer_type"],
            remove_layer=["layer_type"],
            rotate_project=["angle"],
            # Toolpath generation
            generate_isolation=[],
            generate_drill=[],
            generate_outline=[],
            generate_mask_expose=[],
            get_layer_geometry=[],
            # Autolevel
            setup_autolevel=[],
            start_autolevel=[],
            stop_autolevel=[],
            get_heightmap=[],
            apply_autolevel=["operation_index"],
            # Double-sided
            set_board_side=["side"],
            mirror_gcode=["operation_index"],
            set_alignment_holes=["holes"],
            probe_alignment=[],
            stop_alignment_probe=[],
            compute_alignment=[],
            apply_alignment=["operation_index"],
            clear_alignment=[],
            # Operations
            get_operations=[],
            download_gcode=["operation_index"],
            delete_operation=["operation_index"],
            reorder_operations=["new_order"],
            get_toolpath_preview=[],
            # G-code execution
            send_to_cnc=["operation_index"],
            stop_execution=[],
            confirm_tool_change=[],
            # Real-time overrides
            send_override=["override_type", "action"],
        )

    def _get_grbl_connector(self):
        """Get the active GRBL connector if connected."""
        from .connector import ConnectedGrblPrinter
        
        if hasattr(self._printer, '_connection'):
            connection = self._printer._connection
            if isinstance(connection, ConnectedGrblPrinter):
                return connection
        return None

    # -----------------------------------------------------------------
    # API Command Dispatch Tables
    # -----------------------------------------------------------------

    # CNC commands don't need the project lock
    _CNC_COMMANDS = frozenset({
        "probe", "home", "unlock", "reset", "hold", "resume",
        "set_wcs", "zero_axis", "goto_zero", "status", "machine_check",
        "set_spindle", "spindle_on", "spindle_off", "jog",
        "send_override", "stop_execution", "confirm_tool_change",
    })

    # Project commands touch shared state — require _project_lock
    _PROJECT_COMMANDS = frozenset({
        "new_project", "get_project", "list_projects", "load_project",
        "delete_project", "import_layer", "remove_layer", "rotate_project",
        "generate_isolation", "generate_drill", "generate_outline",
        "generate_mask_expose", "get_layer_geometry",
        "setup_autolevel", "start_autolevel", "stop_autolevel",
        "get_heightmap", "apply_autolevel",
        "set_board_side", "mirror_gcode",
        "set_alignment_holes", "probe_alignment", "stop_alignment_probe",
        "compute_alignment", "apply_alignment", "clear_alignment",
        "get_operations", "download_gcode", "delete_operation",
        "reorder_operations", "get_toolpath_preview",
        "send_to_cnc",
    })

    def on_api_command(self, command, data):
        import flask

        if command in self._CNC_COMMANDS:
            handler = getattr(self, f"_handle_{command}", None)
            if handler:
                return handler(data)

        if command in self._PROJECT_COMMANDS:
            with self._project_lock:
                handler = getattr(self, f"_handle_{command}", None)
                if handler:
                    return handler(data)

        return flask.jsonify(error=gettext("Unknown command")), 400
    
    # -----------------------------------------------------------------
    # Helper: require active GRBL connection
    # -----------------------------------------------------------------

    _VALID_AXES = frozenset({"X", "Y", "Z"})
    _VALID_ZERO_AXES = frozenset({"X", "Y", "Z", "all"})
    _VALID_WCS = frozenset({"G54", "G55", "G56", "G57", "G58", "G59"})

    def _require_grbl_connection(self):
        """Return (connector, None) or (None, error_response)."""
        import flask
        connector = self._get_grbl_connector()
        if connector is None:
            return None, (flask.jsonify(error=gettext("Not connected to GRBL")), 409)
        return connector, None

    def _is_probe_active(self):
        return self._probe_thread is not None and self._probe_thread.is_alive()

    def _is_streaming_active(self, connector):
        if connector is None:
            return False
        checker = getattr(connector, "is_streaming", None)
        if callable(checker):
            return bool(checker())
        stream_thread = getattr(connector, "_stream_thread", None)
        return stream_thread is not None and stream_thread.is_alive()

    def _parse_layer_type(self, layer_type_raw):
        import flask
        try:
            return LayerType(layer_type_raw), None
        except (TypeError, ValueError):
            return None, (flask.jsonify(error=gettext("Invalid layer type: %(value)s") % {"value": layer_type_raw}), 400)

    def _resolve_unique_upload_path(self, dest_dir, safe_name):
        """Return a non-colliding upload target path in dest_dir."""
        base, ext = os.path.splitext(safe_name)
        candidate = os.path.join(dest_dir, safe_name)
        counter = 1
        while os.path.exists(candidate):
            candidate = os.path.join(dest_dir, f"{base}_{counter}{ext}")
            counter += 1
        return candidate

    # -----------------------------------------------------------------
    # CNC Control Handlers
    # -----------------------------------------------------------------

    def _handle_probe(self, data):
        import flask
        axis = data.get("axis", "Z")
        if axis not in self._VALID_AXES:
            return flask.jsonify(error=gettext("Invalid probe axis: %(value)s") % {"value": axis}), 400
        connector, err = self._require_grbl_connection()
        if err:
            return err
        depth = self._settings.get_float(["probe_depth"])
        feed = self._settings.get_int(["probe_feed"])
        connector.probe(axis, depth, feed)
        return flask.jsonify(result="probing")

    def _handle_home(self, data):
        import flask
        connector, err = self._require_grbl_connection()
        if err:
            return err
        connector.home()
        return flask.jsonify(result="homing")

    def _handle_unlock(self, data):
        import flask
        connector, err = self._require_grbl_connection()
        if err:
            return err
        connector.unlock()
        return flask.jsonify(result="unlocked")

    def _handle_reset(self, data):
        import flask
        connector, err = self._require_grbl_connection()
        if err:
            return err
        connector.emergency_stop()
        return flask.jsonify(result="reset")

    def _handle_hold(self, data):
        import flask
        connector, err = self._require_grbl_connection()
        if err:
            return err
        connector.hold()
        return flask.jsonify(result="hold")

    def _handle_resume(self, data):
        import flask
        connector, err = self._require_grbl_connection()
        if err:
            return err
        connector.resume()
        return flask.jsonify(result="resumed")

    def _handle_set_wcs(self, data):
        import flask
        wcs = data.get("wcs", "G54")
        if wcs not in self._VALID_WCS:
            return flask.jsonify(error=gettext("Invalid WCS: %(value)s") % {"value": wcs}), 400
        connector, err = self._require_grbl_connection()
        if err:
            return err
        connector.set_wcs(wcs)
        return flask.jsonify(result=f"WCS set to {wcs}")

    def _handle_zero_axis(self, data):
        import flask
        axis = data.get("axis", "all")
        if axis not in self._VALID_ZERO_AXES:
            return flask.jsonify(error=gettext("Invalid axis: %(value)s") % {"value": axis}), 400
        connector, err = self._require_grbl_connection()
        if err:
            return err
        connector.zero_axis(axis)
        return flask.jsonify(result=f"{axis} zeroed")

    def _handle_goto_zero(self, data):
        import flask
        connector, err = self._require_grbl_connection()
        if err:
            return err
        safe_z = self._settings.get_float(["safe_z_height"])
        z_feed = self._settings.get_float(["z_max_feed"])
        connector.commands(f"G1 Z{safe_z} F{z_feed:.0f}", "G0 X0 Y0", f"G1 Z0 F{z_feed:.0f}")
        return flask.jsonify(result="going to zero")

    def _handle_status(self, data):
        return self._handle_machine_check(data)

    def _build_machine_check_report(self, connector):
        snapshot = {
            "connected": False,
            "state": "Unknown",
            "machine_pos": {"x": 0.0, "y": 0.0, "z": 0.0},
            "work_pos": {"x": 0.0, "y": 0.0, "z": 0.0},
            "work_offset": {"x": 0.0, "y": 0.0, "z": 0.0},
            "feed": 0,
            "spindle": 0,
            "wcs": "G54",
            "z_zeroed": False,
            "streaming": False,
        }
        if connector is not None and hasattr(connector, "machine_snapshot"):
            snapshot.update(connector.machine_snapshot())

        state = str(snapshot.get("state", "Unknown"))
        state_upper = state.upper()
        streaming = bool(snapshot.get("streaming", False)) or self._is_streaming_active(connector)
        probing = self._is_probe_active()

        checks = []

        def add_check(name, ok, message, severity="info"):
            checks.append({
                "name": name,
                "ok": bool(ok),
                "severity": severity,
                "message": message,
            })

        connected = bool(snapshot.get("connected", False))
        add_check(
            "connection",
            connected,
            gettext("Connected to GRBL") if connected else gettext("Not connected to GRBL"),
            "success" if connected else "error",
        )

        if not connected:
            add_check("state", False, gettext("Connection is required before machine checks can run"), "error")
        elif state_upper == "ALARM":
            add_check("state", False, gettext("GRBL is in ALARM state (%(state)s)") % {"state": state}, "error")
        elif state_upper == "IDLE":
            add_check("state", True, gettext("GRBL is idle and ready"), "success")
        else:
            add_check("state", False, gettext("GRBL is busy or paused (%(state)s)") % {"state": state}, "warning")

        add_check(
            "streaming",
            not streaming,
            gettext("No active stream") if not streaming else gettext("A G-code stream is active"),
            "success" if not streaming else "error",
        )
        add_check(
            "probing",
            not probing,
            gettext("No active probe") if not probing else gettext("A probe run is active"),
            "success" if not probing else "error",
        )

        ready = all(check["ok"] for check in checks)
        summary = "ready" if ready else "attention required"

        return {
            "summary": summary,
            "ready": ready,
            "snapshot": snapshot,
            "checks": checks,
            "limitations": [
                gettext("This check verifies controller state and workflow safety."),
                gettext("Step loss, backlash, and absolute accuracy require external measurement such as a dial indicator, gauge block, or probe pad."),
            ],
        }

    def _handle_machine_check(self, data):
        import flask

        connector = self._get_grbl_connector()
        if connector is not None:
            connector.request_status()
            time.sleep(0.05)

        report = self._build_machine_check_report(connector)
        return flask.jsonify(result="machine_check", report=report)

    def _handle_set_spindle(self, data):
        import flask
        try:
            speed = int(data.get("speed", 10000))
        except (TypeError, ValueError):
            return flask.jsonify(error=gettext("Speed must be a number")), 400
        if speed < 0 or speed > 30000:
            return flask.jsonify(error=gettext("Speed out of range (0-30000): %(value)s") % {"value": speed}), 400
        connector, err = self._require_grbl_connection()
        if err:
            return err
        connector.set_spindle(speed, on=True)
        return flask.jsonify(result=f"spindle speed set to {speed}")

    def _handle_spindle_on(self, data):
        import flask
        connector, err = self._require_grbl_connection()
        if err:
            return err
        speed = self._settings.get_int(["default_spindle_speed"])
        connector.set_spindle(speed, on=True)
        return flask.jsonify(result="spindle on")

    def _handle_spindle_off(self, data):
        import flask
        connector, err = self._require_grbl_connection()
        if err:
            return err
        connector.set_spindle(0, on=False)
        return flask.jsonify(result="spindle off")

    def _handle_jog(self, data):
        import flask
        axis = data.get("axis", "X")
        if axis not in self._VALID_AXES:
            return flask.jsonify(error=gettext("Invalid jog axis: %(value)s") % {"value": axis}), 400
        try:
            distance = float(data.get("distance", 1.0))
        except (TypeError, ValueError):
            return flask.jsonify(error=gettext("Distance must be a number")), 400
        if abs(distance) > 500:
            return flask.jsonify(error=gettext("Jog distance too large: %(value)s") % {"value": distance}), 400
        try:
            feed = int(data.get("feed", self._settings.get_int(["default_jog_feed"])))
        except (TypeError, ValueError):
            return flask.jsonify(error=gettext("Feed must be a number")), 400
        if feed < 1 or feed > 10000:
            return flask.jsonify(error=gettext("Feed out of range (1-10000): %(value)s") % {"value": feed}), 400
        connector, err = self._require_grbl_connection()
        if err:
            return err
        connector.jog({axis.lower(): distance}, relative=True, speed=feed)
        return flask.jsonify(result=f"jogging {axis}")
    
    # -----------------------------------------------------------------
    # Project Command Handlers (caller holds _project_lock)
    # -----------------------------------------------------------------

    def _handle_new_project(self, data):
        import flask
        name = data.get("name", "Untitled PCB")
        self._project = PCBProject(name=name)
        self._geometries = {}
        self._geometries_unrotated = {}
        self._drills_unrotated = []
        self._height_map = None
        self._save_project()
        self._logger.info(f"New PCB project: {self._project.id}")
        return flask.jsonify(result="ok", project=self._project.to_dict())

    def _handle_get_project(self, data):
        import flask
        if self._project is None:
            return flask.jsonify(result="no_project", project=None)
        project_dict = self._project.to_dict()
        # Return live height_map data if available (may be fresher than
        # what is saved in the project, e.g. during an active probe run).
        if self._height_map is not None:
            project_dict["height_map"] = self._height_map.to_dict()
            project_dict["has_height_map"] = True
        return flask.jsonify(result="ok", project=project_dict)

    def _handle_list_projects(self, data):
        import flask
        projects = list_projects(self._upload_dir)
        return flask.jsonify(result="ok", projects=projects)

    def _handle_remove_layer(self, data):
        import flask
        if self._project is None:
            return flask.jsonify(error=gettext("No project")), 400
        lt, err = self._parse_layer_type(data.get("layer_type"))
        if err:
            return err
        if lt in self._project.layers:
            del self._project.layers[lt]
            self._geometries.pop(lt, None)
            self._geometries_unrotated.pop(lt, None)
        self._save_project()
        return flask.jsonify(result="ok", project=self._project.to_dict())

    def _handle_stop_autolevel(self, data):
        import flask
        self._probe_stop.set()
        return flask.jsonify(result="stopping")

    def _handle_get_heightmap(self, data):
        import flask
        if self._height_map is None:
            return flask.jsonify(result="no_heightmap", heightmap=None)
        return flask.jsonify(
            result="ok", heightmap=self._height_map.to_dict()
        )

    def _handle_set_board_side(self, data):
        import flask
        if self._project is None:
            return flask.jsonify(error=gettext("No project")), 400
        side = data.get("side", "single")
        try:
            self._project.board_side = BoardSide(side)
        except (TypeError, ValueError):
            return flask.jsonify(error=gettext("Invalid board side: %(value)s") % {"value": side}), 400
        self._project.flip_axis = data.get("flip_axis", "X")
        self._save_project()
        return flask.jsonify(result="ok", project=self._project.to_dict())

    def _handle_stop_alignment_probe(self, data):
        import flask
        self._probe_stop.set()
        return flask.jsonify(result="ok")

    def _handle_get_operations(self, data):
        import flask
        if self._project is None:
            return flask.jsonify(result="no_project", operations=[])
        return flask.jsonify(
            result="ok",
            operations=[op.to_dict() for op in self._project.operations],
        )

    # -------------------------------------------------------------------------
    # BlueprintPlugin - File Upload
    # -------------------------------------------------------------------------

    @octoprint.plugin.BlueprintPlugin.route("/upload", methods=["POST"])
    def handle_upload(self):
        """Handle Gerber/Excellon file uploads."""
        import flask
        import shutil

        with self._project_lock:
            return self._handle_upload_inner(flask, shutil)

    def _handle_upload_inner(self, flask, shutil):
        """Inner upload handler (caller holds _project_lock)."""
        if self._project is None:
            self._project = PCBProject()

        # Destination is the per-project subdirectory
        dest_dir = self._get_project_upload_dir()

        # OctoPrint's upload middleware pre-processes multipart file uploads:
        # Instead of flask.request.files, the file is saved to a temp path
        # and metadata is placed in flask.request.values as:
        #   <input_name>.path  - temp file path
        #   <input_name>.name  - original filename
        #   <input_name>.size  - file size
        #   <input_name>.content_type - MIME type
        path_suffix = self._settings.global_get(["server", "uploads", "pathSuffix"]) or "path"
        name_suffix = self._settings.global_get(["server", "uploads", "nameSuffix"]) or "name"

        uploaded_files = []

        # Discover uploaded file keys by looking for *.path entries in form values
        file_keys = set()
        for key in flask.request.values:
            if key.endswith("." + path_suffix):
                base_key = key[: -(len(path_suffix) + 1)]
                file_keys.add(base_key)

        self._logger.info(f"Upload: found {len(file_keys)} file(s): {file_keys}")

        for base_key in sorted(file_keys):
            upload_path = flask.request.values.get(base_key + "." + path_suffix)
            upload_name = flask.request.values.get(base_key + "." + name_suffix, "unknown")

            if not upload_path or not os.path.exists(upload_path):
                self._logger.warning(f"Upload path not found for {base_key}: {upload_path}")
                continue

            # Copy from OctoPrint's temp location to the project directory
            safe_name = os.path.basename(upload_name)
            dest_path = self._resolve_unique_upload_path(dest_dir, safe_name)
            shutil.move(upload_path, dest_path)

            uploaded_files.append({
                "filename": upload_name,
                "filepath": dest_path,
                "layer_type": None,
            })

            # Auto-detect layer type
            lt = guess_layer_type(upload_name)
            if lt:
                uploaded_files[-1]["layer_type"] = lt.value
                self._parse_and_add_layer(dest_path, lt)

            self._logger.info(f"Uploaded: {upload_name} -> {lt}")

        # Auto-save after upload
        self._save_project()

        return flask.jsonify(
            result="ok",
            files=uploaded_files,
            project=self._project.to_dict(),
        )

    def is_blueprint_csrf_protected(self):
        return True

    def bodysize_hook(self, current_max_body_sizes, *args, **kwargs):
        """Allow large file uploads (50MB) on the upload endpoint."""
        return [("POST", r"/upload", 50 * 1024 * 1024)]

    # -------------------------------------------------------------------------
    # PCB Workflow Handlers
    # -------------------------------------------------------------------------

    def _parse_and_add_layer(self, filepath: str, layer_type: LayerType):
        """Parse a file and add it to the project."""
        import copy
        try:
            if layer_type == LayerType.DRILL:
                layer, drills = parse_excellon_file(filepath)
                layer.layer_type = layer_type
                self._project.add_layer(layer)
                self._project.drills = drills
                # Keep unrotated copy of drill data
                self._drills_unrotated = copy.deepcopy(drills)
                self._logger.info(
                    f"Added drill layer: {len(drills)} tools, "
                    f"{sum(d.count for d in drills)} holes"
                )
            else:
                layer, geometries = parse_gerber_file(filepath)
                layer.layer_type = layer_type
                self._project.add_layer(layer)
                self._geometries[layer_type] = geometries
                # Keep unrotated copy
                self._geometries_unrotated[layer_type] = list(geometries)
                self._logger.info(
                    f"Added {layer_type.value} layer: "
                    f"{len(geometries)} geometries"
                )
            self._project.state = ProjectState.PARSED

            # If the project already has rotation applied, re-apply it so
            # the newly added layer is rotated together with the others.
            if self._project.rotation and self._project.rotation != 0:
                self._apply_rotation_to_geometries(self._project.rotation)
        except Exception as e:
            self._logger.error(f"Error parsing {filepath}: {e}")
            self._logger.debug(traceback.format_exc())
            raise

    def _handle_import_layer(self, data):
        """Handle import_layer API command."""
        import flask

        if self._project is None:
            self._project = PCBProject()

        filename = os.path.basename(data.get("filename", ""))
        layer_type, err = self._parse_layer_type(data.get("layer_type"))
        if err:
            return err
        # Look in the per-project directory first, then base upload dir
        project_dir = self._get_project_upload_dir()
        filepath = os.path.join(project_dir, filename)
        if not os.path.exists(filepath):
            filepath = os.path.join(self._upload_dir, filename)

        if not os.path.exists(filepath):
            return flask.jsonify(error=gettext("File not found: %(value)s") % {"value": filename}), 404

        try:
            self._parse_and_add_layer(filepath, layer_type)
            self._save_project()
            return flask.jsonify(result="ok", project=self._project.to_dict())
        except Exception as e:
            self._logger.error(f"Import layer failed: {e}")
            return flask.jsonify(error=str(e)), 500

    def _handle_load_project(self, data):
        """Load an existing project by ID."""
        import flask

        project_id = data.get("project_id")
        if not project_id:
            return flask.jsonify(error=gettext("No project_id provided")), 400

        project_dir = os.path.realpath(os.path.join(self._upload_dir, project_id))
        if not project_dir.startswith(os.path.realpath(self._upload_dir) + os.sep):
            return flask.jsonify(error=gettext("Invalid project ID")), 400
        project_json = os.path.join(project_dir, "project.json")
        if not os.path.isfile(project_json):
            return flask.jsonify(error=gettext("Project not found: %(value)s") % {"value": project_id}), 404

        try:
            self._project = PCBProject.load(project_dir)
            # Re-parse geometries from layer files
            self._geometries = {}
            for lt, layer in self._project.layers.items():
                if os.path.isfile(layer.filepath):
                    try:
                        if lt == LayerType.DRILL:
                            _, drills = parse_excellon_file(layer.filepath)
                            self._project.drills = drills
                        else:
                            parsed_layer, geometries = parse_gerber_file(
                                layer.filepath
                            )
                            self._geometries[lt] = geometries
                            # Update layer bounds from re-parsed geometry
                            # (may differ from saved data if parser improved)
                            layer.min_x = parsed_layer.min_x
                            layer.min_y = parsed_layer.min_y
                            layer.max_x = parsed_layer.max_x
                            layer.max_y = parsed_layer.max_y
                            layer.width = parsed_layer.width
                            layer.height = parsed_layer.height
                    except Exception as e:
                        self._logger.warning(
                            f"Could not re-parse {layer.filepath}: {e}"
                        )

            # Recalculate board bounds from updated layers
            self._project._update_board_dimensions()

            # Snapshot unrotated state before applying stored rotation
            self._snapshot_unrotated()

            # Re-apply stored rotation to freshly parsed geometries
            if self._project.rotation != 0:
                self._apply_rotation_to_geometries(self._project.rotation)

            # Restore height map if present
            if self._project.height_map:
                self._height_map = HeightMap.from_dict(self._project.height_map)
            else:
                self._height_map = None

            self._logger.info(
                f"Loaded project: {self._project.name} ({self._project.id})"
                f", rotation={self._project.rotation}°"
            )
            # Touch project.json so this becomes the "last used" project
            self._save_project()
            return flask.jsonify(result="ok", project=self._project.to_dict())
        except Exception as e:
            self._logger.error(f"Error loading project {project_id}: {e}")
            return flask.jsonify(error=str(e)), 500

    def _handle_delete_project(self, data):
        """Delete a project by ID."""
        import flask

        project_id = data.get("project_id")
        if not project_id:
            return flask.jsonify(error=gettext("No project_id provided")), 400

        # If deleting the currently loaded project, clear it
        if self._project and self._project.id == project_id:
            self._project = None
            self._geometries = {}
            self._geometries_unrotated = {}
            self._drills_unrotated = []
            self._height_map = None

        deleted = delete_project(self._upload_dir, project_id)
        if not deleted:
            return flask.jsonify(error=gettext("Project not found: %(value)s") % {"value": project_id}), 404

        # Return updated project list
        projects = list_projects(self._upload_dir)
        return flask.jsonify(result="ok", projects=projects)

    def _snapshot_unrotated(self):
        """Save copies of the current (unrotated) geometries and drills.

        Called right after (re-)parsing files, before any rotation is applied.
        This ensures that subsequent rotations always start from clean data,
        avoiding floating-point drift from incremental rotation.
        """
        import copy
        self._geometries_unrotated = {lt: list(geoms) if isinstance(geoms, list) else geoms
                                      for lt, geoms in self._geometries.items()}
        self._drills_unrotated = copy.deepcopy(self._project.drills) if self._project else []

    def _apply_rotation_to_geometries(self, angle):
        """Apply a rotation (degrees CCW) to all in-memory geometries and drill holes.

        Always computes from the stored unrotated originals to avoid
        floating-point drift from repeated incremental rotations.
        """
        from shapely import affinity
        from shapely.ops import unary_union
        import copy
        import math

        # Start from unrotated originals
        source = self._geometries_unrotated if self._geometries_unrotated else self._geometries
        if not source:
            return

        # Restore unrotated copies into _geometries
        self._geometries = {lt: list(geoms) if isinstance(geoms, list) else geoms
                            for lt, geoms in source.items()}

        # Restore unrotated drills
        if self._drills_unrotated:
            self._project.drills = copy.deepcopy(self._drills_unrotated)

        if angle == 0:
            self._project._update_board_dimensions()
            return

        # --- Find the center of all geometry for rotation pivot ---
        all_geoms = []
        for lt, geoms in self._geometries.items():
            if isinstance(geoms, list):
                all_geoms.extend([g for g in geoms if g is not None and not g.is_empty])
            elif geoms is not None and not geoms.is_empty:
                all_geoms.append(geoms)

        if not all_geoms:
            return

        combined = unary_union(all_geoms)
        cx, cy = combined.centroid.x, combined.centroid.y

        # --- Rotate all Shapely geometries ---
        for lt in list(self._geometries.keys()):
            geoms = self._geometries[lt]
            if isinstance(geoms, list):
                rotated = []
                for g in geoms:
                    if g is not None and not g.is_empty:
                        rotated.append(affinity.rotate(g, angle, origin=(cx, cy)))
                    else:
                        rotated.append(g)
                self._geometries[lt] = rotated
            elif geoms is not None and not geoms.is_empty:
                self._geometries[lt] = affinity.rotate(geoms, angle, origin=(cx, cy))

        # --- Find bounding box of all rotated geometry ---
        all_rotated = []
        for lt, geoms in self._geometries.items():
            if isinstance(geoms, list):
                all_rotated.extend([g for g in geoms if g is not None and not g.is_empty])
            elif geoms is not None and not geoms.is_empty:
                all_rotated.append(geoms)

        rotated_union = unary_union(all_rotated)
        rmin_x, rmin_y, rmax_x, rmax_y = rotated_union.bounds

        # --- Translate so min corner is at (0,0) - stay in quadrant 1 ---
        dx = -rmin_x
        dy = -rmin_y
        if abs(dx) > 0.0001 or abs(dy) > 0.0001:
            for lt in list(self._geometries.keys()):
                geoms = self._geometries[lt]
                if isinstance(geoms, list):
                    self._geometries[lt] = [
                        affinity.translate(g, dx, dy)
                        if g is not None and not g.is_empty else g
                        for g in geoms
                    ]
                elif geoms is not None and not geoms.is_empty:
                    self._geometries[lt] = affinity.translate(geoms, dx, dy)

        # --- Rotate drill hole positions ---
        rad = math.radians(angle)
        cos_a, sin_a = math.cos(rad), math.sin(rad)

        for drill in self._project.drills:
            rotated_holes = []
            for hx, hy in drill.holes:
                rx = cos_a * (hx - cx) - sin_a * (hy - cy) + cx
                ry = sin_a * (hx - cx) + cos_a * (hy - cy) + cy
                rotated_holes.append((rx + dx, ry + dy))
            drill.holes = rotated_holes

        # --- Update layer bounds from rotated geometry ---
        for lt, layer in self._project.layers.items():
            geoms = self._geometries.get(lt)
            if geoms:
                if isinstance(geoms, list):
                    valid = [g for g in geoms if g is not None and not g.is_empty]
                    if valid:
                        u = unary_union(valid)
                        bx0, by0, bx1, by1 = u.bounds
                    else:
                        continue
                else:
                    bx0, by0, bx1, by1 = geoms.bounds
                layer.min_x = bx0
                layer.min_y = by0
                layer.max_x = bx1
                layer.max_y = by1
                layer.width = bx1 - bx0
                layer.height = by1 - by0

        # --- Update board dimensions ---
        self._project._update_board_dimensions()

        self._logger.info(
            f"Applied rotation {angle}°: board now "
            f"{self._project.board_width:.2f} x {self._project.board_height:.2f} mm"
        )

    def _handle_rotate_project(self, data):
        """Rotate all project geometry by the given angle (degrees CCW).
        
        Always computes from unrotated originals to prevent drift.
        Translates result so board origin stays at (0,0) in quadrant 1.
        Clears any existing operations (they need re-generation).
        """
        import flask

        if self._project is None:
            return flask.jsonify(error=gettext("No project loaded")), 400

        angle = float(data.get("angle", 90))
        if angle == 0:
            return flask.jsonify(result="ok", project=self._project.to_dict())

        self._logger.info(f"Rotating project by {angle}°")

        # Accumulate rotation for persistence
        self._project.rotation = (self._project.rotation + angle) % 360

        # Apply total rotation from unrotated originals
        self._apply_rotation_to_geometries(self._project.rotation)

        # --- Clear existing operations (stale after rotation) ---
        if self._project.operations:
            self._logger.info(
                f"Clearing {len(self._project.operations)} operations "
                f"(stale after rotation)"
            )
            self._project.operations.clear()

        # --- 9) Clear height map (probe data is position-dependent) ---
        self._height_map = None
        self._project.height_map = None

        self._save_project()

        self._logger.info(
            f"Rotation complete: board now "
            f"{self._project.board_width:.2f} x {self._project.board_height:.2f} mm"
        )

        return flask.jsonify(
            result="ok",
            project=self._project.to_dict(),
            angle=angle,
            board_width=self._project.board_width,
            board_height=self._project.board_height,
        )

    def _get_toolpath_settings(self, data=None) -> ToolpathSettings:
        """Build ToolpathSettings from plugin settings + optional overrides."""
        s = ToolpathSettings(
            tool_diameter=self._settings.get_float(["tool_diameter"]),
            tool_angle=self._settings.get_float(["tool_angle"]),
            cut_depth=self._settings.get_float(["cut_depth"]),
            feed_rate=self._settings.get_float(["default_feed_rate"]),
            plunge_rate=self._settings.get_float(["plunge_rate"]),
            spindle_speed=self._settings.get_int(["default_spindle_speed"]),
            safe_z=self._settings.get_float(["safe_z_height"]),
            travel_z=self._settings.get_float(["travel_z_height"]),
            passes=self._settings.get_int(["isolation_passes"]),
            pass_overlap=self._settings.get_float(["isolation_overlap"]),
            depth_per_pass=self._settings.get_float(["depth_per_pass"]),
            max_width=self._settings.get_float(["isolation_max_width"]),
            clear_gaps=self._settings.get_boolean(["isolation_clear_gaps"]),
            peck_depth=self._settings.get_float(["peck_depth"]),
            drill_feed=self._settings.get_float(["drill_feed"]),
            breakthrough_depth=self._settings.get_float(["breakthrough_depth"]),
            tab_width=self._settings.get_float(["outline_tab_width"]),
            tab_height=self._settings.get_float(["outline_tab_height"]),
            num_tabs=self._settings.get_int(["outline_num_tabs"]),
            spindle_dwell=self._settings.get_float(["spindle_delay"]),
            wcs=self._settings.get(["wcs_default"]),
            z_max_feed=self._settings.get_float(["z_max_feed"]),
            # UV Mask Exposure
            uv_offset_x=self._settings.get_float(["uv_led_offset_x"]),
            uv_offset_y=self._settings.get_float(["uv_led_offset_y"]),
            uv_spot_size=self._settings.get_float(["uv_spot_size"]),
            uv_expose_speed=self._settings.get_float(["uv_expose_speed"]),
            uv_expose_z=self._settings.get_float(["uv_expose_z"]),
            uv_power=self._settings.get_int(["uv_power"]),
        )
        # Apply overrides from request data
        if data:
            for key in (
                "tool_diameter", "tool_angle", "cut_depth", "depth_per_pass",
                "feed_rate", "plunge_rate", "spindle_speed",
                "passes", "pass_overlap", "max_width", "clear_gaps", "safe_z", "travel_z",
                "peck_depth", "drill_feed", "breakthrough_depth",
                "tab_width", "tab_height", "num_tabs",
                "spindle_dwell",
                "wcs",
                "z_max_feed",
                "uv_offset_x", "uv_offset_y", "uv_spot_size",
                "uv_expose_speed", "uv_expose_z", "uv_power",
            ):
                if key in data:
                    setattr(s, key, type(getattr(s, key))(data[key]))
        return s

    def _handle_generate_isolation(self, data):
        """Generate isolation milling G-code."""
        import flask

        if self._project is None:
            return flask.jsonify(error=gettext("No project")), 400

        # Determine which layer(s) to process
        layer_type = LayerType(data.get("layer_type", "top_copper"))
        geometries = self._geometries.get(layer_type)
        if not geometries:
            return flask.jsonify(error=gettext("No geometry for %(value)s") % {"value": layer_type.value}), 400

        settings = self._get_toolpath_settings(data)

        # Merge geometries and generate toolpath
        copper = merge_geometries(geometries)
        gcode = generate_isolation_gcode(
            copper,
            settings,
            origin_x=self._project.board_min_x,
            origin_y=self._project.board_min_y,
        )

        # Create operation
        op = MillingOperation(
            name=f"Isolation - {layer_type.value}",
            operation_type="isolation",
            layer_type=layer_type.value,
            gcode=gcode,
            tool_diameter=settings.tool_diameter,
            tool_angle=settings.tool_angle,
            cut_depth=settings.cut_depth,
            feed_rate=settings.feed_rate,
            spindle_speed=settings.spindle_speed,
            passes=settings.passes,
            pass_overlap=settings.pass_overlap,
        )
        # Replace existing operation of the same type/layer, or append
        replaced = False
        operation_index = len(self._project.operations)  # default: will be appended
        for i, existing in enumerate(self._project.operations):
            if existing.operation_type == "isolation" and existing.layer_type == layer_type.value:
                self._project.operations[i] = op
                replaced = True
                operation_index = i
                break
        if not replaced:
            self._project.operations.append(op)
        self._project.state = ProjectState.TOOLPATHS_GENERATED
        self._save_project()

        self._logger.info(f"Generated isolation G-code: {len(gcode)} chars")

        return flask.jsonify(
            result="ok",
            operation=op.to_dict(),
            operation_index=operation_index,
            gcode_lines=gcode.count("\n") + 1,
        )

    def _handle_get_layer_geometry(self, data):
        """Return serialised polygon geometry for canvas rendering."""
        import flask
        from shapely.geometry import Polygon

        if self._project is None:
            return flask.jsonify(error=gettext("No project")), 400

        ox = self._project.board_min_x
        oy = self._project.board_min_y
        simplify_tol = 0.01  # 10 µm — invisible on canvas, big data saving

        result = {}
        layer_type_str = data.get("layer_type")
        targets = {}
        if layer_type_str:
            lt = LayerType(layer_type_str)
            geoms = self._geometries.get(lt)
            if geoms:
                targets[lt] = geoms
        else:
            targets = dict(self._geometries)

        for lt, geoms in targets.items():
            merged = merge_geometries(geoms)
            if merged.is_empty:
                continue
            polys = []
            for poly in merged.geoms:
                sp = poly.simplify(simplify_tol, preserve_topology=True)
                if sp.is_empty or not isinstance(sp, Polygon):
                    continue
                exterior = [
                    [round(x - ox, 4), round(y - oy, 4)]
                    for x, y in sp.exterior.coords
                ]
                interiors = []
                for ring in sp.interiors:
                    interiors.append([
                        [round(x - ox, 4), round(y - oy, 4)]
                        for x, y in ring.coords
                    ])
                polys.append({"exterior": exterior, "interiors": interiors})
            if polys:
                result[lt.value] = polys

        return flask.jsonify(result="ok", geometries=result)

    def _handle_generate_drill(self, data):
        """Generate drilling G-code."""
        import flask

        if self._project is None:
            return flask.jsonify(error=gettext("No project loaded")), 400
        if not self._project.drills:
            return flask.jsonify(
                error=gettext("Drill file contains no holes (0 tools, 0 holes)")
            ), 400

        settings = self._get_toolpath_settings(data)
        settings.cut_depth = float(data.get(
            "board_thickness",
            self._settings.get_float(["board_thickness"]),
        ))

        gcode = generate_drill_gcode(
            self._project.drills,
            settings,
            origin_x=self._project.board_min_x,
            origin_y=self._project.board_min_y,
        )

        op = MillingOperation(
            name="Drilling",
            operation_type="drill",
            layer_type="drill",
            gcode=gcode,
            tool_diameter=0,  # varies by tool
            cut_depth=settings.cut_depth,
            feed_rate=settings.drill_feed,
            spindle_speed=settings.spindle_speed,
        )
        # Replace existing drill operation, or append
        replaced = False
        operation_index = len(self._project.operations)  # default: will be appended
        for i, existing in enumerate(self._project.operations):
            if existing.operation_type == "drill":
                self._project.operations[i] = op
                replaced = True
                operation_index = i
                break
        if not replaced:
            self._project.operations.append(op)
        self._save_project()

        return flask.jsonify(
            result="ok",
            operation=op.to_dict(),
            operation_index=operation_index,
            gcode_lines=gcode.count("\n") + 1,
        )

    def _handle_generate_outline(self, data):
        """Generate outline cutting G-code."""
        import flask

        if self._project is None:
            return flask.jsonify(error=gettext("No project")), 400

        outline_geom = self._geometries.get(LayerType.OUTLINE)
        if not outline_geom:
            # No outline layer — use board bounding rectangle as fallback
            from shapely.geometry import box

            if (
                self._project.board_width > 0
                and self._project.board_height > 0
            ):
                outline_geom = box(
                    self._project.board_min_x,
                    self._project.board_min_y,
                    self._project.board_min_x + self._project.board_width,
                    self._project.board_min_y + self._project.board_height,
                )
                self._logger.info(
                    "No outline layer — using board bounding box "
                    f"({self._project.board_width:.2f} x "
                    f"{self._project.board_height:.2f} mm)"
                )
            else:
                return flask.jsonify(
                    error=gettext("No outline layer and no board dimensions")
                ), 400
        else:
            outline_geom = merge_geometries(outline_geom)

        settings = self._get_toolpath_settings(data)
        board_thickness = data.get(
            "board_thickness",
            self._settings.get_float(["board_thickness"]),
        )

        gcode = generate_outline_gcode(
            outline_geom,
            settings,
            board_thickness=board_thickness,
            origin_x=self._project.board_min_x,
            origin_y=self._project.board_min_y,
        )

        op = MillingOperation(
            name="Outline Cut",
            operation_type="outline",
            layer_type="outline",
            gcode=gcode,
            tool_diameter=settings.tool_diameter,
            cut_depth=board_thickness,
            feed_rate=settings.feed_rate,
            spindle_speed=settings.spindle_speed,
        )
        # Replace existing outline operation, or append
        replaced = False
        operation_index = len(self._project.operations)  # default: will be appended
        for i, existing in enumerate(self._project.operations):
            if existing.operation_type == "outline":
                self._project.operations[i] = op
                replaced = True
                operation_index = i
                break
        if not replaced:
            self._project.operations.append(op)
        self._save_project()

        return flask.jsonify(
            result="ok",
            operation=op.to_dict(),
            operation_index=operation_index,
            gcode_lines=gcode.count("\n") + 1,
        )

    def _handle_generate_mask_expose(self, data):
        """Generate UV solder mask exposure G-code."""
        import flask

        if self._project is None:
            return flask.jsonify(error=gettext("No project")), 400

        layer_type_str = data.get("layer_type", "top_mask")
        try:
            layer_type = LayerType(layer_type_str)
        except ValueError:
            return flask.jsonify(error=gettext("Invalid layer type: %(value)s") % {"value": layer_type_str}), 400

        mask_geom = self._geometries.get(layer_type)
        if mask_geom:
            mask_merged = merge_geometries(mask_geom)
        else:
            mask_merged = None  # expose entire board (no openings)

        if self._project.board_width <= 0 or self._project.board_height <= 0:
            return flask.jsonify(error=gettext("No board dimensions available")), 400

        board_bounds = (
            self._project.board_min_x,
            self._project.board_min_y,
            self._project.board_min_x + self._project.board_width,
            self._project.board_min_y + self._project.board_height,
        )

        settings = self._get_toolpath_settings(data)

        gcode = generate_mask_expose_gcode(
            mask_merged,
            board_bounds,
            settings,
            origin_x=self._project.board_min_x,
            origin_y=self._project.board_min_y,
        )

        if not gcode:
            return flask.jsonify(
                error=gettext("Generated empty G-code: nothing to expose")
            ), 400

        op = MillingOperation(
            name=f"Mask Expose - {layer_type.value}",
            operation_type="mask_expose",
            layer_type=layer_type.value,
            gcode=gcode,
            tool_diameter=settings.uv_spot_size,
            feed_rate=settings.uv_expose_speed,
            spindle_speed=settings.uv_power,
        )
        # Replace existing operation of the same type/layer, or append
        replaced = False
        operation_index = len(self._project.operations)
        for i, existing in enumerate(self._project.operations):
            if (
                existing.operation_type == "mask_expose"
                and existing.layer_type == layer_type.value
            ):
                self._project.operations[i] = op
                replaced = True
                operation_index = i
                break
        if not replaced:
            self._project.operations.append(op)
        self._save_project()

        return flask.jsonify(
            result="ok",
            operation=op.to_dict(),
            operation_index=operation_index,
            gcode_lines=gcode.count("\n") + 1,
        )

    # -------------------------------------------------------------------------
    # Autolevel Handlers
    # -------------------------------------------------------------------------

    def _handle_setup_autolevel(self, data):
        """Set up autolevel grid from board dimensions."""
        import flask

        if self._project is None:
            return flask.jsonify(error=gettext("No project")), 400

        grid_x = data.get("grid_x", self._settings.get_int(["autolevel_grid_x"]))
        grid_y = data.get("grid_y", self._settings.get_int(["autolevel_grid_y"]))
        margin = data.get("margin", self._settings.get_float(["autolevel_margin"]))

        self._height_map = HeightMap()
        # Use CNC coordinates (0,0 origin at board corner) to match G-code
        self._height_map.generate_grid(
            min_x=0,
            min_y=0,
            max_x=self._project.board_width,
            max_y=self._project.board_height,
            grid_x=grid_x,
            grid_y=grid_y,
            margin=margin,
        )

        self._project.height_map = self._height_map.to_dict()
        self._discard_leveled_gcode()
        self._save_project()

        return flask.jsonify(
            result="ok",
            heightmap=self._height_map.to_dict(),
            total_points=len(self._height_map.points),
        )

    def _handle_start_autolevel(self, data):
        """Start the autolevel probing sequence."""
        import flask

        if self._height_map is None:
            return flask.jsonify(error=gettext("No height map configured")), 400

        connector = self._get_grbl_connector()
        if not connector:
            return flask.jsonify(error=gettext("Not connected to GRBL")), 400

        if self._is_probe_active():
            return flask.jsonify(error=gettext("Probing already in progress")), 409

        if self._is_streaming_active(connector):
            return flask.jsonify(error=gettext("Cannot start probing while streaming is active")), 409

        # The new heights replace the old ones: leveled G-code from the
        # previous measurement must not be sent any more
        self._height_map.work_offset = dict(connector._work_offset)
        self._discard_leveled_gcode()
        self._save_project()

        # Start probing in background thread
        self._probe_stop.clear()
        self._probe_thread = threading.Thread(
            target=self._autolevel_probe_loop,
            args=(connector,),
            daemon=True,
        )
        self._probe_thread.start()

        return flask.jsonify(result="ok", message=gettext("Probing started"))

    PROBE_TIMEOUT = 30.0

    def _await_probe_result(self, comm, work_offset):
        """
        Wait for the result of one G38.2 probe.

        Returns (z, None) with z in work coordinates on contact,
        (None, reason) on failure ("no_contact", "grbl_error", "alarm",
        "timeout") and (None, None) if the user stopped probing.
        """
        z_val = None
        deadline = time.time() + self.PROBE_TIMEOUT
        while time.time() < deadline:
            if self._probe_stop.is_set():
                return None, None
            try:
                resp = comm._response_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if resp[0] == "probe":
                if not resp[1]["success"]:
                    return None, "no_contact"
                # PRB reports machine coordinates; convert to work
                # coordinates so Z is about 0 at the board surface
                z_val = resp[1]["z"] - work_offset.get("z", 0.0)
            elif resp[0] == "ok":
                if z_val is not None:
                    return z_val, None
            elif resp[0] == "error":
                return None, "grbl_error"
            elif resp[0] == "alarm":
                return None, "alarm"
        return None, "timeout"

    def _autolevel_probe_loop(self, connector):
        """Background thread for sequential probing."""
        probe_depth = self._settings.get_float(["probe_depth"])
        probe_feed = self._settings.get_float(["probe_feed"])
        safe_z = self._settings.get_float(["safe_z_height"])
        travel_z = self._settings.get_float(["travel_z_height"])
        z_feed = self._settings.get_float(["z_max_feed"])
        # Use minimal clearance between probe points to reduce Z travel
        # and therefore reduce accumulated lost-step drift.
        inter_point_z = travel_z + 1.0  # just enough clearance for XY moves

        total = len(self._height_map.points)
        self._logger.info(f"Starting autolevel probing: {total} points")

        # Enable stream mode on the comm to capture responses
        comm = connector._comm
        if comm:
            comm._stream_mode = True
            # Drain stale responses
            while not comm._response_queue.empty():
                try:
                    comm._response_queue.get_nowait()
                except queue.Empty:
                    break

        try:
            for i, point in enumerate(self._height_map.points):
                if self._probe_stop.is_set():
                    self._logger.info("Probing stopped by user")
                    break

                if point.probed:
                    continue

                # Move to point — use minimal Z clearance to reduce Z travel
                connector.commands(
                    f"G1 Z{inter_point_z} F{z_feed:.0f}",
                    f"G0 X{point.x:.4f} Y{point.y:.4f}",
                    f"G1 Z{travel_z} F{z_feed:.0f}",
                )
                # Wait for the 3 movement commands to complete
                for _ in range(3):
                    self._wait_for_ok(comm, timeout=10.0)

                # Probe
                connector.commands(
                    "G21",
                    "G90",
                    f"G38.2 Z{probe_depth} F{probe_feed}",
                )
                # Wait for G21 and G90 ok's
                self._wait_for_ok(comm, timeout=2.0)
                self._wait_for_ok(comm, timeout=2.0)

                z_val, failure = self._await_probe_result(comm, connector._work_offset)
                if failure:
                    # Never record a substitute height: the point stays
                    # unprobed and the run stops so the user can fix the cause
                    self._logger.error(f"Autolevel aborted at point {i}: {failure}")
                    self._probe_stop.set()
                    if self._plugin_manager:
                        self._plugin_manager.send_plugin_message(
                            "grblpcb",
                            {"type": "autolevel_failed", "index": i, "total": total, "reason": failure},
                        )
                    break
                if z_val is None:
                    # Stopped by the user while waiting
                    break

                self._height_map.set_probe_result(i, z_val)

                # Persist intermediate results so a browser refresh
                # (or server restart) can recover the partial height map.
                with self._project_lock:
                    self._project.height_map = self._height_map.to_dict()
                    # Save to disk every 5 points to limit I/O
                    if (i + 1) % 5 == 0 or (i + 1) == total:
                        self._save_project()

                # Retract
                connector.commands(f"G1 Z{travel_z} F{z_feed:.0f}")
                self._wait_for_ok(comm, timeout=5.0)
                # Send progress update
                if self._plugin_manager:
                    self._plugin_manager.send_plugin_message(
                        "grblpcb",
                        {
                            "type": "autolevel_progress",
                            "index": i,
                            "total": total,
                            "point": point.to_dict(),
                            "percent": int((i + 1) / total * 100),
                        },
                    )

        except Exception as e:
            self._logger.error(f"Error during probing: {e}")
            self._logger.debug(traceback.format_exc())

        # Build interpolator when done (still inside stream_mode)
        z_drift = None
        if self._height_map.is_complete():
            self._height_map.build_interpolator()

            # ── Z-drift verification ──
            # Re-probe the first point and compare with the original reading.
            # Any significant difference indicates the Z-axis lost steps during
            # the probing cycle.  Must happen while stream_mode is still active
            # so we can read probe responses from the queue.
            try:
                # Re-enable stream mode in case finally already ran
                if comm is not None:
                    comm._stream_mode = True

                first_pt = self._height_map.points[0]
                self._logger.info(
                    "Drift check: re-probing point 0 "
                    f"(X{first_pt.x:.3f} Y{first_pt.y:.3f}, "
                    f"original Z={first_pt.z:.4f})"
                )
                connector.commands(
                    f"G1 Z{inter_point_z} F{z_feed:.0f}",
                    f"G0 X{first_pt.x:.4f} Y{first_pt.y:.4f}",
                    f"G1 Z{travel_z} F{z_feed:.0f}",
                )
                for _ in range(3):
                    self._wait_for_ok(comm, timeout=10.0)

                connector.commands(
                    "G21", "G90",
                    f"G38.2 Z{probe_depth} F{probe_feed}",
                )
                self._wait_for_ok(comm, timeout=2.0)
                self._wait_for_ok(comm, timeout=2.0)

                # Read the drift-check probe result
                drift_z = None
                deadline = time.time() + 30.0
                while time.time() < deadline:
                    try:
                        resp = comm._response_queue.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    if resp[0] == "probe":
                        pd = resp[1]
                        if pd["success"]:
                            wco_z = connector._work_offset.get("z", 0.0)
                            drift_z = pd["z"] - wco_z
                        else:
                            self._logger.warning("Drift check probe failed (no contact)")
                            break
                    elif resp[0] == "ok":
                        if drift_z is not None:
                            break
                    elif resp[0] in ("error", "alarm"):
                        self._logger.warning(f"Drift check probe error: {resp}")
                        break

                if drift_z is not None:
                    z_drift = drift_z - first_pt.z
                    self._logger.info(
                        f"Z-drift check: original={first_pt.z:.4f}, "
                        f"re-probe={drift_z:.4f}, drift={z_drift:.4f}mm"
                    )
                    if abs(z_drift) > 0.05:
                        self._logger.warning(
                            f"⚠ Z-axis drift of {z_drift:.3f}mm detected! "
                            f"The Z motor likely lost steps during probing. "
                            f"Consider reducing Z acceleration ($122) or "
                            f"max Z rate ($112)."
                        )
                        # Force re-zero before milling
                        connector._z_zeroed = False
                else:
                    self._logger.warning(
                        "Drift check: no probe result received (timeout)"
                    )
                # Retract after drift check
                connector.commands(f"G1 Z{travel_z} F{z_feed:.0f}")
                self._wait_for_ok(comm, timeout=5.0)
            except Exception as e:
                self._logger.warning(f"Z-drift check failed: {e}")
                import traceback as tb
                self._logger.debug(tb.format_exc())

        # Now disable stream mode
        try:
            if comm is not None and hasattr(comm, '_stream_mode'):
                comm._stream_mode = False
        except Exception as e:
            self._logger.debug(f"Error resetting stream mode: {e}")

        if self._height_map.is_complete():
            with self._project_lock:
                self._project.state = ProjectState.PROBED
                self._project.height_map = self._height_map.to_dict()
                self._save_project()
            self._logger.info("Autolevel probing complete, interpolator built")

            if self._plugin_manager:
                msg = {
                    "type": "autolevel_complete",
                    "heightmap": self._height_map.to_dict(),
                }
                if z_drift is not None:
                    msg["z_drift"] = round(z_drift, 4)
                    # The frontend words the warning (translated)
                    msg["z_drift_exceeded"] = abs(z_drift) > 0.05
                    msg["probed_points"] = total
                self._plugin_manager.send_plugin_message(
                    "grblpcb", msg,
                )

        # Return to safe position
        connector.commands(
            f"G1 Z{safe_z} F{z_feed:.0f}",
            "G0 X0 Y0",
        )

    def _wait_for_ok(self, comm, timeout=5.0):
        """Wait for an 'ok' response from GRBL."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = comm._response_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if resp[0] == "ok":
                return True
            elif resp[0] == "error":
                return False
        return False

    def _discard_leveled_gcode(self):
        """Drop leveled G-code of all operations (height map changed)."""
        if self._project is None:
            return
        for op in self._project.operations:
            op.gcode_leveled = ""

    def _handle_apply_autolevel(self, data):
        """Apply autolevel correction to a generated G-code operation."""
        import flask

        if self._height_map is None or not self._height_map.completed:
            return flask.jsonify(error=gettext("Height map not complete")), 400

        op_index = data.get("operation_index", 0)
        if self._project is None or op_index >= len(self._project.operations):
            return flask.jsonify(error=gettext("Invalid operation index")), 400

        op = self._project.operations[op_index]
        if not op.gcode:
            return flask.jsonify(error=gettext("No G-code to correct")), 400

        max_segment = data.get(
            "max_segment",
            self._settings.get_float(["autolevel_max_segment"]),
        )

        leveled = apply_autolevel(op.gcode, self._height_map, max_segment)
        op.gcode_leveled = leveled
        self._project.state = ProjectState.LEVELED
        self._save_project()

        return flask.jsonify(
            result="ok",
            operation=op.to_dict(),
            gcode_lines=leveled.count("\n") + 1,
        )

    # -------------------------------------------------------------------------
    # Mirror / Double-sided Handlers
    # -------------------------------------------------------------------------

    def _handle_mirror_gcode(self, data):
        """Mirror G-code for bottom-side milling."""
        import flask

        if self._project is None:
            return flask.jsonify(error=gettext("No project")), 400

        op_index = data.get("operation_index", 0)
        if op_index >= len(self._project.operations):
            return flask.jsonify(error=gettext("Invalid operation index")), 400

        op = self._project.operations[op_index]
        source = op.gcode  # Always mirror raw gcode, not leveled (to avoid double-correction)
        if not source:
            return flask.jsonify(error=gettext("No G-code to mirror")), 400

        flip_axis = data.get("flip_axis", self._project.flip_axis)
        if flip_axis == "X":
            mirrored = mirror_coordinates_x(source, self._project.board_width)
        else:
            mirrored = mirror_coordinates_y(source, self._project.board_height)

        # Create new operation for mirrored version
        mirrored_op = MillingOperation(
            name=f"{op.name} (mirrored)",
            operation_type=op.operation_type,
            layer_type=op.layer_type,
            gcode=mirrored,
            tool_diameter=op.tool_diameter,
            tool_angle=op.tool_angle,
            cut_depth=op.cut_depth,
            feed_rate=op.feed_rate,
            spindle_speed=op.spindle_speed,
            passes=op.passes,
        )

        # Replace existing mirrored version if found, otherwise insert after source
        existing_idx = None
        for i, existing_op in enumerate(self._project.operations):
            if existing_op.name == mirrored_op.name and i != op_index:
                existing_idx = i
                break
        if existing_idx is not None:
            self._project.operations[existing_idx] = mirrored_op
            mirror_index = existing_idx
        else:
            self._project.operations.insert(op_index + 1, mirrored_op)
            mirror_index = op_index + 1
        self._save_project()

        return flask.jsonify(
            result="ok",
            operation=mirrored_op.to_dict(),
            operation_index=mirror_index,
        )

    # -------------------------------------------------------------------------
    # Alignment (double-sided board compensation)
    # -------------------------------------------------------------------------

    def _handle_set_alignment_holes(self, data):
        """Set which drill holes to use as alignment references.

        Expects: holes = [{expected_x, expected_y, diameter}, ...]
        These are the design-space positions of the alignment holes.
        After mirroring, expected_x is flipped by board_width.
        """
        import flask

        if self._project is None:
            return flask.jsonify(error=gettext("No project")), 400

        holes = data.get("holes", [])
        if len(holes) < 2:
            return flask.jsonify(error=gettext("Need at least 2 alignment holes")), 400

        self._project.alignment_holes = [
            {
                "expected_x": float(h["expected_x"]),
                "expected_y": float(h["expected_y"]),
                "diameter": float(h.get("diameter", 1.0)),
                "measured_x": None,
                "measured_y": None,
                "probed": False,
            }
            for h in holes
        ]
        self._project.alignment_transform = None
        self._save_project()

        self._logger.info(
            f"Set {len(holes)} alignment holes: "
            + ", ".join(f"({h['expected_x']:.2f}, {h['expected_y']:.2f})" for h in holes)
        )

        return flask.jsonify(
            result="ok",
            alignment_holes=self._project.alignment_holes,
        )

    def _handle_probe_alignment(self, data):
        """Start XY edge-find probing of alignment holes.

        Probes each unprobed alignment hole to find its actual center.
        The bit is lowered into the hole and probed in ±X then ±Y.
        """
        import flask

        if self._project is None:
            return flask.jsonify(error=gettext("No project")), 400

        if len(self._project.alignment_holes) < 2:
            return flask.jsonify(error=gettext("No alignment holes set")), 400

        connector = self._get_grbl_connector()
        if not connector:
            return flask.jsonify(error=gettext("Not connected to GRBL")), 400

        if self._is_probe_active():
            return flask.jsonify(error=gettext("Probing already in progress")), 409

        if self._is_streaming_active(connector):
            return flask.jsonify(error=gettext("Cannot start probing while streaming is active")), 409

        # Start probing in background thread
        self._probe_stop.clear()
        self._probe_thread = threading.Thread(
            target=self._alignment_probe_loop,
            args=(connector,),
            daemon=True,
        )
        self._probe_thread.start()

        return flask.jsonify(result="ok", message=gettext("Alignment probing started"))

    def _alignment_probe_loop(self, connector):
        """Background thread: probe each alignment hole to find its XY center.

        For each hole:
        1. Rapid to expected XY at safe Z
        2. Plunge into hole (to probe_depth inside the hole)
        3. Probe X+ → record contact
        4. Retract to center, probe X-  → record contact
        5. Move to center_x
        6. Probe Y+ → record, retract, probe Y- → record
        7. Compute center = midpoint of contacts
        8. Retract to safe Z
        """
        safe_z = self._settings.get_float(["safe_z_height"])
        probe_feed = self._settings.get_float(["probe_feed"])
        z_feed = self._settings.get_float(["z_max_feed"])
        # Probe depth into the hole (negative Z, should be shallow — about
        # half board thickness so the bit is inside the plated hole)
        hole_probe_z = -(self._settings.get_float(["board_thickness"]) * 0.5)

        comm = connector._comm
        if comm:
            comm._stream_mode = True
            while not comm._response_queue.empty():
                try:
                    comm._response_queue.get_nowait()
                except queue.Empty:
                    break

        holes = self._project.alignment_holes
        total = len(holes)

        try:
            for idx, hole in enumerate(holes):
                if self._probe_stop.is_set():
                    self._logger.info("Alignment probing stopped by user")
                    break

                ex = hole["expected_x"]
                ey = hole["expected_y"]
                radius = hole["diameter"] / 2.0
                # Search distance: bit travels from center toward wall
                # Use 80% of radius as the max travel (bit has finite diameter)
                search_dist = radius * 0.8

                self._logger.info(
                    f"Probing alignment hole {idx+1}/{total} "
                    f"at ({ex:.3f}, {ey:.3f}) d={hole['diameter']:.2f}"
                )

                # -- Move to hole position at safe Z --
                connector.commands(
                    f"G1 Z{safe_z} F{z_feed:.0f}",
                    f"G0 X{ex:.4f} Y{ey:.4f}",
                )
                for _ in range(2):
                    self._wait_for_ok(comm, timeout=10.0)

                # -- Plunge into hole --
                connector.commands(f"G1 Z{hole_probe_z:.4f} F{z_feed:.0f}")
                self._wait_for_ok(comm, timeout=10.0)

                # -- Probe X+ --
                x_plus = self._probe_axis(
                    comm, connector, "X", ex + search_dist, probe_feed
                )
                if x_plus is None:
                    self._logger.warning(f"X+ probe failed at hole {idx}")
                    continue

                # Return to center
                connector.commands(f"G0 X{ex:.4f}")
                self._wait_for_ok(comm, timeout=5.0)

                # -- Probe X- --
                x_minus = self._probe_axis(
                    comm, connector, "X", ex - search_dist, probe_feed
                )
                if x_minus is None:
                    self._logger.warning(f"X- probe failed at hole {idx}")
                    continue

                center_x = (x_plus + x_minus) / 2.0

                # Move to measured center X
                connector.commands(f"G0 X{center_x:.4f}")
                self._wait_for_ok(comm, timeout=5.0)

                # -- Probe Y+ --
                y_plus = self._probe_axis(
                    comm, connector, "Y", ey + search_dist, probe_feed
                )
                if y_plus is None:
                    self._logger.warning(f"Y+ probe failed at hole {idx}")
                    continue

                # Return to center Y
                connector.commands(f"G0 Y{ey:.4f}")
                self._wait_for_ok(comm, timeout=5.0)

                # -- Probe Y- --
                y_minus = self._probe_axis(
                    comm, connector, "Y", ey - search_dist, probe_feed
                )
                if y_minus is None:
                    self._logger.warning(f"Y- probe failed at hole {idx}")
                    continue

                center_y = (y_plus + y_minus) / 2.0

                # Store result
                hole["measured_x"] = round(center_x, 4)
                hole["measured_y"] = round(center_y, 4)
                hole["probed"] = True

                self._logger.info(
                    f"Hole {idx+1}: expected ({ex:.4f}, {ey:.4f}) "
                    f"measured ({center_x:.4f}, {center_y:.4f}) "
                    f"Δx={center_x - ex:.4f} Δy={center_y - ey:.4f}"
                )

                # Retract
                connector.commands(f"G1 Z{safe_z} F{z_feed:.0f}")
                self._wait_for_ok(comm, timeout=5.0)

                # Send progress
                if self._plugin_manager:
                    self._plugin_manager.send_plugin_message(
                        "grblpcb",
                        {
                            "type": "alignment_progress",
                            "index": idx,
                            "total": total,
                            "hole": hole,
                            "percent": int((idx + 1) / total * 100),
                        },
                    )

        except Exception as e:
            self._logger.error(f"Error during alignment probing: {e}")
            self._logger.debug(traceback.format_exc())
        finally:
            try:
                if comm is not None and hasattr(comm, '_stream_mode'):
                    comm._stream_mode = False
            except Exception as e:
                self._logger.debug(f"Error resetting stream mode: {e}")

        # Save results
        with self._project_lock:
            self._save_project()

        # Check if all holes probed
        all_probed = all(h["probed"] for h in holes)

        if self._plugin_manager:
            self._plugin_manager.send_plugin_message(
                "grblpcb",
                {
                    "type": "alignment_complete",
                    "all_probed": all_probed,
                    "holes": holes,
                },
            )

    def _probe_axis(self, comm, connector, axis: str, target: float, feed: float):
        """Probe a single axis direction, return the contact coordinate or None."""
        connector.commands(
            "G21", "G90",
            f"G38.2 {axis}{target:.4f} F{feed}",
        )
        # Wait for G21, G90 ok
        self._wait_for_ok(comm, timeout=2.0)
        self._wait_for_ok(comm, timeout=2.0)

        # Wait for probe result
        result_val = None
        deadline = time.time() + 15.0
        while time.time() < deadline and not self._probe_stop.is_set():
            try:
                resp = comm._response_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if resp[0] == "probe":
                probe_data = resp[1]
                if probe_data["success"]:
                    # Extract the relevant axis coordinate
                    result_val = probe_data.get(axis.lower(), None)
                else:
                    self._logger.warning(f"Probe {axis} contact failed")
                    return None
            elif resp[0] == "ok":
                if result_val is not None:
                    return result_val
            elif resp[0] == "error":
                self._logger.error(f"Probe {axis} error: {resp[1]}")
                return None
            elif resp[0] == "alarm":
                self._logger.error(f"Alarm during probe: {resp[2]}")
                self._probe_stop.set()
                return None

        return result_val

    def _handle_compute_alignment(self, data):
        """Compute alignment transform from probed hole positions."""
        import flask

        if self._project is None:
            return flask.jsonify(error=gettext("No project")), 400

        holes = self._project.alignment_holes
        probed = [h for h in holes if h.get("probed")]
        if len(probed) < 2:
            return flask.jsonify(
                error=gettext("Need at least 2 probed holes, have %(value)s") % {"value": len(probed)}
            ), 400

        expected = [(h["expected_x"], h["expected_y"]) for h in probed]
        measured = [(h["measured_x"], h["measured_y"]) for h in probed]

        try:
            transform = compute_alignment_transform(expected, measured)
        except (ValueError, ArithmeticError) as e:
            return flask.jsonify(error=gettext("Transform computation failed: %(value)s") % {"value": e}), 400

        self._project.alignment_transform = transform.to_dict()
        self._save_project()

        return flask.jsonify(
            result="ok",
            transform=transform.to_dict(),
            message=(
                f"Alignment: rotation {transform.theta_deg:.3f}°, "
                f"offset ({transform.tx:.4f}, {transform.ty:.4f})mm, "
                f"RMS error {transform.rms_error:.4f}mm"
            ),
        )

    def _handle_apply_alignment(self, data):
        """Apply the computed alignment transform to an operation's G-code."""
        import flask

        if self._project is None:
            return flask.jsonify(error=gettext("No project")), 400

        if self._project.alignment_transform is None:
            return flask.jsonify(error=gettext("No alignment transform computed")), 400

        op_index = data.get("operation_index", 0)
        if op_index >= len(self._project.operations):
            return flask.jsonify(error=gettext("Invalid operation index")), 400

        op = self._project.operations[op_index]
        # Apply to the best available G-code (leveled > original)
        source = op.gcode_leveled if op.gcode_leveled else op.gcode
        if not source:
            return flask.jsonify(error=gettext("No G-code to transform")), 400

        transform = AlignmentTransform.from_dict(self._project.alignment_transform)
        aligned = apply_alignment_transform(source, transform)

        # Store in gcode_leveled (alignment is the last transform before execution)
        op.gcode_leveled = aligned
        self._save_project()

        self._logger.info(
            f"Applied alignment to '{op.name}': "
            f"θ={transform.theta_deg:.3f}° "
            f"tx={transform.tx:.4f} ty={transform.ty:.4f}"
        )

        return flask.jsonify(
            result="ok",
            operation=op.to_dict(),
            message=gettext("Alignment applied: %(angle).3f° rotation, (%(tx).4f, %(ty).4f) mm offset") % {
                "angle": transform.theta_deg, "tx": transform.tx, "ty": transform.ty,
            },
        )

    def _handle_clear_alignment(self, data):
        """Clear alignment data (holes + transform)."""
        import flask

        if self._project is None:
            return flask.jsonify(error=gettext("No project")), 400

        for hole in self._project.alignment_holes:
            hole["measured_x"] = None
            hole["measured_y"] = None
            hole["probed"] = False
        self._project.alignment_transform = None
        self._save_project()

        return flask.jsonify(result="ok")

    def _handle_download_gcode(self, data):
        """Download G-code for an operation."""
        import flask

        if self._project is None:
            return flask.jsonify(error=gettext("No project")), 400

        op_index = data.get("operation_index", 0)
        if op_index >= len(self._project.operations):
            return flask.jsonify(error=gettext("Invalid operation index")), 400

        op = self._project.operations[op_index]
        # Prefer leveled G-code if available
        gcode = op.gcode_leveled if op.gcode_leveled else op.gcode
        if not gcode:
            return flask.jsonify(error=gettext("No G-code")), 400

        return flask.jsonify(
            result="ok",
            gcode=gcode,
            filename=f"{self._project.name}_{op.name}.gcode".replace(" ", "_"),
        )

    def _handle_delete_operation(self, data):
        """Delete a specific operation by index."""
        import flask

        if self._project is None:
            return flask.jsonify(error=gettext("No project")), 400

        op_index = data.get("operation_index", 0)
        if op_index < 0 or op_index >= len(self._project.operations):
            return flask.jsonify(error=gettext("Invalid operation index")), 400

        removed = self._project.operations.pop(op_index)
        self._save_project()
        self._logger.info(f"Deleted operation: {removed.name}")

        return flask.jsonify(
            result="ok",
            project=self._project.to_dict(),
        )

    def _handle_reorder_operations(self, data):
        """Reorder operations by providing a new index order."""
        import flask

        if self._project is None:
            return flask.jsonify(error=gettext("No project")), 400

        new_order = data.get("new_order", [])
        ops = self._project.operations
        if sorted(new_order) != list(range(len(ops))):
            return flask.jsonify(error=gettext("Invalid order - must be permutation of indices")), 400

        self._project.operations = [ops[i] for i in new_order]
        self._save_project()

        return flask.jsonify(
            result="ok",
            project=self._project.to_dict(),
        )

    def _handle_send_to_cnc(self, data):
        """Send an operation's G-code to the CNC for execution."""
        import flask

        if self._project is None:
            return flask.jsonify(error=gettext("No project")), 400

        connector = self._get_grbl_connector()
        if not connector:
            return flask.jsonify(error=gettext("Not connected to GRBL")), 400

        if self._is_probe_active():
            return flask.jsonify(error=gettext("Cannot start streaming while probing is active")), 409

        op_index = data.get("operation_index", 0)
        if op_index < 0 or op_index >= len(self._project.operations):
            return flask.jsonify(error=gettext("Invalid operation index")), 400

        op = self._project.operations[op_index]
        # Prefer leveled G-code if available
        gcode = op.gcode_leveled if op.gcode_leveled else op.gcode
        if not gcode:
            return flask.jsonify(error=gettext("No G-code to execute")), 400

        # Pre-flight checks; the frontend shows the warnings by code and
        # resends with force=True once the user confirms
        if not data.get("force", False):
            warnings = []
            op_type = (op.operation_type or "").lower()
            needs_z_accuracy = op_type in {"isolation", "drill", "outline", "cut"}
            if needs_z_accuracy and not getattr(connector, "_z_zeroed", False):
                warnings.append({"code": "z_not_zeroed"})
            if op.gcode_leveled and self._height_map is not None:
                origin = height_map_origin_check(self._height_map, connector._work_offset)
                if origin:
                    warnings.append(origin)
            if warnings:
                return flask.jsonify(result="warnings", warnings=warnings)

        success = connector.stream_gcode(gcode, operation_name=op.name)
        if not success:
            return flask.jsonify(error=gettext("Failed to start streaming")), 400

        return flask.jsonify(
            result="ok",
            message=op.name,
            total_lines=gcode.count("\n") + 1,
        )

    def _handle_stop_execution(self, data):
        """Stop the current G-code execution."""
        import flask

        connector = self._get_grbl_connector()
        if not connector:
            return flask.jsonify(error=gettext("Not connected to GRBL")), 400

        connector.stop_streaming()
        return flask.jsonify(result="ok", message=gettext("Execution stopped"))

    def _handle_confirm_tool_change(self, data):
        """Confirm tool change during execution."""
        import flask

        connector = self._get_grbl_connector()
        if not connector:
            return flask.jsonify(error=gettext("Not connected to GRBL")), 400

        connector.confirm_tool_change()
        return flask.jsonify(result="ok", message=gettext("Tool change confirmed"))

    def _handle_send_override(self, data):
        """Send a real-time override command to GRBL."""
        import flask

        connector = self._get_grbl_connector()
        if not connector:
            return flask.jsonify(error=gettext("Not connected to GRBL")), 400

        override_type = data.get("override_type", "")
        action = data.get("action", "")

        valid_types = {"feed", "rapid", "spindle"}
        valid_actions = {"reset", "plus10", "minus10", "plus1", "minus1", "100", "50", "25"}

        if override_type not in valid_types:
            return flask.jsonify(error=gettext("Invalid override type: %(value)s") % {"value": override_type}), 400
        if action not in valid_actions:
            return flask.jsonify(error=gettext("Invalid action: %(value)s") % {"value": action}), 400

        success = connector.send_override(override_type, action)
        if not success:
            return flask.jsonify(error=gettext("Failed to send override")), 400

        return flask.jsonify(result="ok")

    def _handle_get_toolpath_preview(self, data):
        """Parse G-code from operations into point arrays for workspace rendering."""
        import flask

        if self._project is None:
            return flask.jsonify(result="no_project", toolpaths=[])

        toolpaths = []
        for op in self._project.operations:
            # Prefer leveled G-code for preview
            gcode = op.gcode_leveled if op.gcode_leveled else op.gcode
            if not gcode:
                continue

            points = []
            cx, cy, cz = 0.0, 0.0, 0.0
            spindle_on = False
            for line in gcode.splitlines():
                line = line.strip()
                if not line or line.startswith(";") or line.startswith("("):
                    continue

                # Track spindle/laser state (M3/M4 = on, M5 = off)
                m_match = re.match(r"^M([345])\b", line, re.IGNORECASE)
                if m_match:
                    m_code = int(m_match.group(1))
                    spindle_on = m_code in (3, 4)
                    continue

                # Parse G0/G1 commands
                g_match = re.match(r"^G([0-3])\b", line, re.IGNORECASE)
                if not g_match:
                    continue

                g_code = int(g_match.group(1))
                is_rapid = g_code == 0

                # Extract X, Y, Z
                x_match = re.search(r"X([-\d.]+)", line, re.IGNORECASE)
                y_match = re.search(r"Y([-\d.]+)", line, re.IGNORECASE)
                z_match = re.search(r"Z([-\d.]+)", line, re.IGNORECASE)

                if x_match:
                    cx = float(x_match.group(1))
                if y_match:
                    cy = float(y_match.group(1))
                if z_match:
                    cz = float(z_match.group(1))

                points.append({
                    "x": cx,
                    "y": cy,
                    "z": cz,
                    "rapid": is_rapid,
                    "spindle": spindle_on,
                })

            if points:
                toolpaths.append({
                    "name": op.name,
                    "operation_type": op.operation_type,
                    "layer_type": op.layer_type,
                    "points": points,
                })

        return flask.jsonify(result="ok", toolpaths=toolpaths)

    # -------------------------------------------------------------------------
    # EventHandlerPlugin
    # -------------------------------------------------------------------------

    def on_event(self, event, payload):
        """Handle OctoPrint events."""
        if event == Events.CONNECTED:
            self._logger.info("Printer connected")
            # Check if it's a GRBL connector
            connector = self._get_grbl_connector()
            if connector:
                self._logger.info("Connected to GRBL CNC")
        
        elif event == Events.DISCONNECTED:
            self._logger.info("Printer disconnected")


# Plugin metadata
__plugin_name__ = "GrblPCB"
__plugin_version__ = "0.1.0"
__plugin_pythoncompat__ = ">=3.11,<4"
__plugin_author__ = "Andreas Hergert"
__plugin_url__ = "https://git.hergert.info/octoprint/OctoPrint-GrblPCB"
__plugin_license__ = "AGPLv3"
__plugin_description__ = "GRBL-based CNC control for PCB isolation milling, drilling, and outline cutting"


def __plugin_check__():
    """Verify that the required OctoPrint ConnectedPrinter API is available."""
    try:
        from octoprint.printer.connection import ConnectedPrinter  # noqa: F401
        return True
    except ImportError:
        return False


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = GrblPcbPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.server.http.bodysize": __plugin_implementation__.bodysize_hook,
    }
