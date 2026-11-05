# -*- coding: utf-8 -*-
"""
GRBL Connector for OctoPrint

This module provides a ConnectedPrinter implementation for GRBL-based CNC machines.
It registers as a separate connector type in OctoPrint's connection system.
"""

import logging
import queue
import threading
import time
from gettext import gettext
from typing import Any

from octoprint.printer import (
    ConnectedPrinterCapabilities,
    FirmwareInformation,
)
from octoprint.printer.connection import (
    ConnectedPrinter,
    ConnectedPrinterState,
)
from octoprint.printer.job import PrintJob

from .grbl_comm import GrblComm, grbl_serial_list


class ConnectedGrblPrinter(ConnectedPrinter):
    """
    OctoPrint connector for GRBL-based CNC machines.
    
    This connector implements the GRBL protocol for CNC mills,
    supporting features like jog, probe, WCS management, and spindle control.
    """
    
    connector = "grbl"
    name = "GRBL CNC Connection"
    
    printer_capabilities = ConnectedPrinterCapabilities(
        job_on_hold=True,
        temperature_offsets=False,  # GRBL doesn't have temperatures
    )
    
    # Injected by plugin
    _event_bus = None
    _file_manager = None
    _plugin_settings = None
    _plugin_manager = None
    
    @classmethod
    def connection_options(cls) -> dict:
        """Return available connection options (ports and baudrates)."""
        return {
            "port": grbl_serial_list(),
            "baudrate": [115200, 250000, 230400, 57600, 38400, 19200, 9600],
        }
    
    @classmethod
    def connection_preconditions_met(cls, params: dict[str, Any]) -> bool:
        """Check if connection preconditions are met."""
        serials = grbl_serial_list()
        port = params.get("port")
        return len(serials) > 0 and (not port or port == "AUTO" or port in serials)

    def request_status(self):
        """Request a live status update from GRBL."""
        if self._comm is not None:
            self._comm.request_status()
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        self._port = kwargs.get("port")
        self._baudrate = kwargs.get("baudrate", 115200)
        
        self._comm: GrblComm = None
        self._grbl_state = "Unknown"
        self._machine_position = {"x": 0.0, "y": 0.0, "z": 0.0}
        self._work_position = {"x": 0.0, "y": 0.0, "z": 0.0}
        self._work_offset = {"x": 0.0, "y": 0.0, "z": 0.0}
        self._feed_rate = 0
        self._spindle_speed = 0
        self._current_wcs = "G54"
        self._z_zeroed = False  # True after user explicitly zeros Z
        
        self._status_poll_thread = None
        self._stop_polling = threading.Event()
        
        # G-code streaming state
        self._stream_thread = None
        self._stream_stop = threading.Event()
        self._stream_pause = threading.Event()
        self._stream_lines = []
        self._stream_current_line = 0
        self._stream_total_lines = 0
        self._stream_start_time = 0
        self._tool_change_event = threading.Event()
        
        self._logger = logging.getLogger(__name__)
    
    @property
    def connection_parameters(self) -> dict:
        """Return current connection parameters."""
        params = super().connection_parameters
        params.update({
            "port": self._port,
            "baudrate": self._baudrate,
        })
        return params
    
    # -------------------------------------------------------------------------
    # Connection Management
    # -------------------------------------------------------------------------
    
    def connect(self, *args, **kwargs):
        """Establish connection to GRBL controller."""
        if self._comm is not None:
            return
        
        self._logger.info(f"Connecting to GRBL on {self._port} @ {self._baudrate}")
        self.state = ConnectedPrinterState.CONNECTING
        
        try:
            self._comm = GrblComm(
                port=self._port,
                baudrate=self._baudrate,
                status_callback=self._on_grbl_status,
                response_callback=self._on_grbl_response,
                error_callback=self._on_grbl_error,
                alarm_callback=self._on_grbl_alarm,
            )
            self._comm.connect()
            
            # Wait for GRBL to wake up and send banner
            time.sleep(2)
            
            # Send soft reset and get status
            self._comm.soft_reset()
            time.sleep(0.5)
            self._comm.request_status()
            
            # Set firmware info
            self.firmware_info = FirmwareInformation(
                name="GRBL",
                data={
                    "FIRMWARE_NAME": "GRBL",
                    "port": self._port,
                    "baudrate": self._baudrate,
                }
            )
            
            self.state = ConnectedPrinterState.OPERATIONAL
            
            # Start status polling
            self._start_status_polling()
            
            self._logger.info("GRBL connection established")
            
        except Exception as e:
            self._logger.error(f"Failed to connect to GRBL: {e}")
            self.state = ConnectedPrinterState.ERROR
            self._comm = None
            raise
    
    def disconnect(self, *args, **kwargs):
        """Disconnect from GRBL controller."""
        self._logger.info("Disconnecting from GRBL")
        
        self._stop_status_polling()
        
        if self._comm is not None:
            self._comm.disconnect()
            self._comm = None
        
        self._z_zeroed = False
        self.state = ConnectedPrinterState.CLOSED
    
    def emergency_stop(self, *args, **kwargs):
        """Emergency stop - GRBL reset."""
        if self._comm:
            self._comm.soft_reset()
    
    # -------------------------------------------------------------------------
    # Status Polling
    # -------------------------------------------------------------------------
    
    def _start_status_polling(self):
        """Start background thread to poll GRBL status."""
        self._stop_polling.clear()
        self._status_poll_thread = threading.Thread(
            target=self._poll_status_loop,
            daemon=True,
        )
        self._status_poll_thread.start()
    
    def _stop_status_polling(self):
        """Stop the status polling thread."""
        self._stop_polling.set()
        if self._status_poll_thread:
            self._status_poll_thread.join(timeout=2.0)
            self._status_poll_thread = None
    
    def _poll_status_loop(self):
        """Background loop to poll GRBL status."""
        while not self._stop_polling.is_set():
            try:
                if self._comm and self._comm.is_connected():
                    self._comm.request_status()
            except (OSError, RuntimeError) as e:
                self._logger.debug(f"Status poll error: {e}")
            
            self._stop_polling.wait(0.25)  # Poll 4x per second
    
    # -------------------------------------------------------------------------
    # GRBL Callbacks
    # -------------------------------------------------------------------------
    
    def _on_grbl_status(self, status: dict):
        """Handle GRBL status update."""
        was_alarm = self._grbl_state == "Alarm"
        self._grbl_state = status.get("state", "Unknown")
        if was_alarm != (self._grbl_state == "Alarm"):
            # Refresh OctoPrint's state text (see get_state_string)
            self._listener.on_printer_state_changed(self.state)
        
        if "mpos" in status:
            self._machine_position = status["mpos"]
        if "wpos" in status:
            self._work_position = status["wpos"]
        if "wco" in status:
            self._work_offset = status["wco"]
        if "fs" in status:
            self._feed_rate = status["fs"].get("feed", 0)
            self._spindle_speed = status["fs"].get("spindle", 0)
        
        # Calculate work position from machine position + offset
        if "mpos" in status and "wco" in status:
            self._work_position = {
                axis: self._machine_position[axis] - self._work_offset[axis]
                for axis in ["x", "y", "z"]
            }
        
        # Notify listeners
        self._listener.on_printer_position_changed(
            {
                "x": self._work_position["x"],
                "y": self._work_position["y"],
                "z": self._work_position["z"],
                "e": 0,  # No extruder
            },
            reason="grbl_status",
        )
        
        # Send plugin message
        if self._plugin_manager:
            msg = {
                    "type": "status",
                    "state": self._grbl_state,
                    "machine_pos": self._machine_position,
                    "work_pos": self._work_position,
                    "work_offset": self._work_offset,
                    "feed": self._feed_rate,
                    "spindle": self._spindle_speed,
                    "wcs": self._current_wcs,
                }
            if "overrides" in status:
                msg["overrides"] = status["overrides"]
            self._plugin_manager.send_plugin_message("grblpcb", msg)
    
    def _on_grbl_response(self, response: str):
        """Handle GRBL response."""
        self._listener.on_printer_logs(f"<<< {response}")
    
    def _on_grbl_error(self, error_code: int, error_msg: str):
        """Handle GRBL error."""
        self._logger.error(f"GRBL error {error_code}: {error_msg}")
        self._listener.on_printer_logs(f"<<< error:{error_code} ({error_msg})")

    def _on_grbl_alarm(self, alarm_code: int, alarm_msg: str):
        """Handle GRBL alarm: log it and tell the user what to do."""
        self._listener.on_printer_logs(f"<<< ALARM:{alarm_code} ({alarm_msg})")
        if self._plugin_manager:
            self._plugin_manager.send_plugin_message(
                "grblpcb", {"type": "alarm", "code": alarm_code, "message": alarm_msg}
            )

    def get_state_string(self, state: ConnectedPrinterState | None = None):
        """While GRBL is in alarm, say so instead of "Operational"."""
        if state is None:
            state = self.state
        if state == ConnectedPrinterState.OPERATIONAL and self._grbl_state == "Alarm":
            return gettext("Alarm (GRBL)")
        return super().get_state_string(state)
    
    # -------------------------------------------------------------------------
    # Command Interface
    # -------------------------------------------------------------------------
    
    def commands(self, *commands, tags=None, force=False, **kwargs):
        """Send G-code commands to GRBL."""
        if self._comm is None:
            return
        
        if len(commands) == 1 and isinstance(commands[0], (list, tuple)):
            commands = commands[0]
        
        for command in commands:
            self._listener.on_printer_logs(f">>> {command}")
            self._comm.send_command(command)
    
    def jog(self, axes: dict, relative=True, speed=None, *args, **kwargs):
        """Jog the CNC machine."""
        if self._comm is None:
            return
        
        # Build jog command - GRBL uses $J= format
        parts = []
        for axis, amount in axes.items():
            parts.append(f"{axis.upper()}{amount}")
        
        if speed is None:
            speed = 500  # Default jog speed
        
        mode = "G91" if relative else "G90"
        jog_cmd = f"$J={mode} G21 {' '.join(parts)} F{speed}"
        
        self._listener.on_printer_logs(f">>> {jog_cmd}")
        self._comm.send_command(jog_cmd)
    
    def home(self, axes=None, *args, **kwargs):
        """Home the CNC machine."""
        if self._comm is None:
            return
        
        # GRBL homes all axes with $H
        self._comm.send_command("$H")
    
    # -------------------------------------------------------------------------
    # GRBL-Specific Commands
    # -------------------------------------------------------------------------
    
    def unlock(self):
        """Unlock GRBL ($X)."""
        if self._comm:
            self._comm.send_command("$X")
    
    def hold(self):
        """Feed hold (!)."""
        if self._comm:
            self._comm.feed_hold()
    
    def resume(self):
        """Resume (~)."""
        if self._comm:
            self._comm.cycle_resume()
    
    def set_wcs(self, wcs: str):
        """Set work coordinate system (G54-G59)."""
        if self._comm and wcs in ("G54", "G55", "G56", "G57", "G58", "G59"):
            self._current_wcs = wcs
            self._comm.send_command(wcs)
    
    def zero_axis(self, axis: str = "all"):
        """Zero axis in current WCS using G10 L20."""
        if self._comm is None:
            return
        
        if axis.lower() == "all":
            self._comm.send_command("G10 L20 P0 X0 Y0 Z0")
            self._z_zeroed = True
        else:
            self._comm.send_command(f"G10 L20 P0 {axis.upper()}0")
            if axis.upper() == "Z":
                self._z_zeroed = True
    
    def probe(self, axis: str = "Z", depth: float = -5.0, feed: float = 25.0):
        """Probe an axis."""
        if self._comm is None:
            return
        
        commands = [
            "G21",  # mm mode
            "G91",  # relative mode
            f"G38.2 {axis.upper()}{depth} F{feed}",  # probe
            f"G0 {axis.upper()}1",  # back off 1mm
            f"G38.2 {axis.upper()}{depth} F{feed / 2}",  # slow probe
            "G90",  # absolute mode
        ]
        
        for cmd in commands:
            self._comm.send_command(cmd)
    
    def set_spindle(self, speed: int, on: bool = True):
        """Set spindle speed and state."""
        if self._comm is None:
            return
        
        if on:
            self._comm.send_command(f"M3 S{speed}")
        else:
            self._comm.send_command("M5")
    
    # -------------------------------------------------------------------------
    # Job Management (simplified for CNC)
    # -------------------------------------------------------------------------
    
    def set_job(self, job: PrintJob, *args, **kwargs):
        """Set the current job."""
        if job is None:
            super().set_job(None)
            return
        
        # Only accept G-code files
        if not job.path.lower().endswith((".gcode", ".nc", ".ngc", ".tap")):
            self._logger.warning(f"Unsupported file type: {job.path}")
            return
        
        super().set_job(job, *args, **kwargs)
    
    def start_print(self, pos=None, user=None, tags=None, *args, **kwargs):
        """Start milling the G-code file."""
        if self._job is None:
            return
        
        self.state = ConnectedPrinterState.PRINTING
        self._listener.on_printer_job_started(user=user)

    def stream_gcode(self, gcode_text: str, operation_name: str = ""):
        """
        Stream G-code text line-by-line to GRBL using character-counting protocol.
        
        This is called by the plugin's send_to_cnc command to execute
        individual operations without going through OctoPrint's file system.
        """
        if self._comm is None:
            return False

        # Reject if a stream is already running
        if self._stream_thread is not None and self._stream_thread.is_alive():
            self._logger.warning("Cannot start stream: another stream is already active")
            return False
        
        # Parse lines
        lines = []
        for line in gcode_text.splitlines():
            line = line.strip()
            # Remove comments
            if ";" in line:
                line = line[:line.index(";")].strip()
            if line.startswith("(") and line.endswith(")"):
                continue
            if not line:
                continue
            lines.append(line)
        
        if not lines:
            return False
        
        self._stream_lines = lines
        self._stream_total_lines = len(lines)
        self._stream_current_line = 0
        self._stream_start_time = time.time()
        self._stream_stop.clear()
        self._stream_pause.clear()
        
        # Start streaming thread
        self._stream_thread = threading.Thread(
            target=self._stream_loop,
            args=(operation_name,),
            daemon=True,
        )
        self._stream_thread.start()
        return True
    
    def stop_streaming(self):
        """Stop the current G-code stream."""
        self._stream_stop.set()
        if self._comm:
            self._comm.feed_hold()
            time.sleep(0.1)
            self._comm.soft_reset()
        self._tool_change_event.set()  # unblock any waiting tool change
    
    def pause_streaming(self):
        """Pause G-code streaming."""
        self._stream_pause.set()
        if self._comm:
            self._comm.feed_hold()
    
    def resume_streaming(self):
        """Resume G-code streaming."""
        self._stream_pause.clear()
        if self._comm:
            self._comm.cycle_resume()
    
    def confirm_tool_change(self):
        """User confirmed tool change - resume streaming."""
        self._tool_change_event.set()

    def is_streaming(self):
        return self._stream_thread is not None and self._stream_thread.is_alive()

    def machine_snapshot(self) -> dict:
        """Return a point-in-time snapshot of the current machine state."""
        return {
            "connected": self._comm is not None and self._comm.is_connected(),
            "state": self._grbl_state,
            "machine_pos": dict(self._machine_position),
            "work_pos": dict(self._work_position),
            "work_offset": dict(self._work_offset),
            "feed": self._feed_rate,
            "spindle": self._spindle_speed,
            "wcs": self._current_wcs,
            "z_zeroed": self._z_zeroed,
            "streaming": self.is_streaming(),
        }

    def _safe_pause_before_manual_intervention(self):
        """Put machine in a safer state before waiting for operator input."""
        if self._comm is None:
            return

        safe_z = 5.0
        z_feed = 120.0
        if self._plugin_settings is not None:
            try:
                safe_z = float(self._plugin_settings.get_float(["safe_z_height"]))
                z_feed = float(self._plugin_settings.get_float(["z_max_feed"]))
            except (TypeError, ValueError, AttributeError):
                pass

        # Force absolute mode so the retract move cannot be interpreted relative.
        self._comm.send_command("G90")
        self._comm.send_command(f"G1 Z{safe_z:.3f} F{z_feed:.0f}")
        self._comm.send_command("M5")

        # Consume best-effort acknowledgements for the 3 safety commands.
        for _ in range(3):
            resp = self._wait_response(timeout=2.0)
            if resp is None:
                break
            if resp[0] == "alarm":
                self._stream_stop.set()
                break

    def _stream_loop(self, operation_name: str):
        """
        Background thread: stream G-code using GRBL character-counting protocol.
        
        GRBL has a 128-byte serial RX buffer. We track how many bytes we've sent
        and wait for 'ok' responses to free buffer space before sending more.
        """
        import re
        
        RX_BUFFER_SIZE = 127
        buffer_fill = 0  # bytes currently in GRBL's RX buffer
        sent_lengths = []  # lengths of commands sent but not yet acknowledged
        
        self._comm._stream_mode = True
        # Drain any stale responses
        while not self._comm._response_queue.empty():
            try:
                self._comm._response_queue.get_nowait()
            except queue.Empty:
                break
        
        self.state = ConnectedPrinterState.PRINTING
        self._stream_error_count = 0
        self._send_stream_progress("started", operation_name)
        
        try:
            for i, line in enumerate(self._stream_lines):
                if self._stream_stop.is_set():
                    break
                
                # Handle pause
                while self._stream_pause.is_set() and not self._stream_stop.is_set():
                    time.sleep(0.1)
                
                if self._stream_stop.is_set():
                    break
                
                # Handle M6 tool change
                if re.match(r"M0?6\b", line, re.IGNORECASE):
                    # Drain buffer before tool change
                    self._drain_buffer(sent_lengths, buffer_fill)
                    buffer_fill = 0
                    sent_lengths = []
                    self._safe_pause_before_manual_intervention()

                    self._send_stream_progress("tool_change", operation_name, line_num=i)
                    self._tool_change_event.clear()
                    # Wait for user confirmation
                    while not self._tool_change_event.is_set() and not self._stream_stop.is_set():
                        time.sleep(0.1)
                    if self._stream_stop.is_set():
                        break
                    self._settle_after_pause()
                    continue  # Skip sending M6 to GRBL (it doesn't understand it)

                # Handle M0 program pause the same way
                if re.match(r"M0\b", line, re.IGNORECASE):
                    self._drain_buffer(sent_lengths, buffer_fill)
                    buffer_fill = 0
                    sent_lengths = []
                    self._safe_pause_before_manual_intervention()

                    self._send_stream_progress("pause", operation_name, line_num=i)
                    self._tool_change_event.clear()
                    while not self._tool_change_event.is_set() and not self._stream_stop.is_set():
                        time.sleep(0.1)
                    if self._stream_stop.is_set():
                        break
                    self._settle_after_pause()
                    continue
                
                cmd_len = len(line) + 1  # +1 for newline
                
                # Wait until there's room in the buffer
                while (buffer_fill + cmd_len) > RX_BUFFER_SIZE:
                    if self._stream_stop.is_set():
                        break
                    resp = self._wait_response(timeout=5.0)
                    if resp is None:
                        continue
                    if resp[0] == "ok" and sent_lengths:
                        buffer_fill -= sent_lengths.pop(0)
                    elif resp[0] == "error":
                        self._logger.error(f"GRBL error at line {i}: {resp[1]} {resp[2]}")
                        self._stream_error_count += 1
                        if sent_lengths:
                            buffer_fill -= sent_lengths.pop(0)
                        # Continue execution despite single-line errors
                    elif resp[0] == "alarm":
                        self._logger.error(f"ALARM during stream: {resp[2]}")
                        self._stream_stop.set()
                        break
                
                if self._stream_stop.is_set():
                    break
                
                # Send the command
                self._comm.send_command(line)
                sent_lengths.append(cmd_len)
                buffer_fill += cmd_len
                self._stream_current_line = i + 1
                
                # Send progress every 10 lines or on last line
                if (i + 1) % 10 == 0 or i == self._stream_total_lines - 1:
                    self._send_stream_progress("progress", operation_name, line_num=i + 1)
            
            # Drain remaining responses
            self._drain_buffer(sent_lengths, buffer_fill)
            
        except Exception as e:
            self._logger.error(f"Stream error: {e}")
            import traceback
            self._logger.debug(traceback.format_exc())
        finally:
            self._comm._stream_mode = False
            self.state = ConnectedPrinterState.OPERATIONAL
            
            error_count = getattr(self, "_stream_error_count", 0)
            if error_count > 0:
                self._logger.warning(f"Stream finished with {error_count} GRBL errors")
            
            if self._stream_stop.is_set():
                self._send_stream_progress("stopped", operation_name)
            else:
                self._send_stream_progress("completed", operation_name)
    
    SETTLE_QUIET = 0.5  # seconds without responses before streaming resumes

    def _settle_after_pause(self):
        """
        Wait until manual commands sent during the pause are done.

        During a tool change the user jogs, probes and zeroes; GRBL answers
        those with ok/[PRB] into the stream queue. Counting them as answers
        to streamed lines would overfill GRBL's 128-byte RX buffer. Discard
        them until GRBL is idle and nothing arrives for SETTLE_QUIET.
        """
        started = quiet_since = time.time()
        reported = False
        while not self._stream_stop.is_set():
            try:
                self._comm._response_queue.get(timeout=0.1)
                quiet_since = time.time()
                continue
            except queue.Empty:
                pass
            if self._grbl_state == "Idle" and time.time() - quiet_since >= self.SETTLE_QUIET:
                return
            if not reported and time.time() - started > 2.0:
                # e.g. GRBL went into alarm during the pause: say why we wait
                self._send_stream_progress("waiting_for_idle", "", line_num=self._stream_current_line)
                reported = True

    def _wait_response(self, timeout: float = 5.0):
        """Wait for a response from the GRBL response queue."""
        try:
            return self._comm._response_queue.get(timeout=timeout)
        except queue.Empty:
            return None
    
    def _drain_buffer(self, sent_lengths, buffer_fill):
        """Wait for all sent commands to be acknowledged."""
        while sent_lengths:
            if self._stream_stop.is_set():
                break
            resp = self._wait_response(timeout=10.0)
            if resp is None:
                continue
            if resp[0] in ("ok", "error") and sent_lengths:
                sent_lengths.pop(0)
            elif resp[0] == "alarm":
                break
    
    def _send_stream_progress(self, status: str, operation_name: str, line_num: int = 0):
        """Send streaming progress to the frontend via plugin message."""
        elapsed = time.time() - self._stream_start_time if self._stream_start_time else 0
        total = self._stream_total_lines
        percent = int(line_num / total * 100) if total > 0 else 0
        
        # Estimate remaining time
        eta = 0
        if line_num > 0 and elapsed > 0:
            rate = line_num / elapsed  # lines per second
            remaining = total - line_num
            eta = remaining / rate if rate > 0 else 0
        
        if self._plugin_manager:
            self._plugin_manager.send_plugin_message(
                "grblpcb",
                {
                    "type": "stream_progress",
                    "status": status,
                    "operation": operation_name,
                    "current_line": line_num,
                    "total_lines": total,
                    "percent": percent,
                    "elapsed": round(elapsed, 1),
                    "eta": round(eta, 1),
                    "errors": getattr(self, "_stream_error_count", 0),
                }
            )
    
    def pause_print(self, user=None, tags=None, *args, **kwargs):
        """Pause milling."""
        if self._comm:
            self._comm.feed_hold()
        self.state = ConnectedPrinterState.PAUSED
        self._listener.on_printer_job_paused(user=user)
    
    def resume_print(self, user=None, tags=None, *args, **kwargs):
        """Resume milling."""
        if self._comm:
            self._comm.cycle_resume()
        self.state = ConnectedPrinterState.PRINTING
        self._listener.on_printer_job_resumed(user=user)
    
    def cancel_print(self, user=None, tags=None, *args, **kwargs):
        """Cancel milling."""
        if self._comm:
            self._comm.soft_reset()
        self.state = ConnectedPrinterState.OPERATIONAL
        self._listener.on_printer_job_cancelled(user=user)
    
    # -------------------------------------------------------------------------
    # Not applicable for CNC but required by interface
    # -------------------------------------------------------------------------
    
    def extrude(self, amount, speed=None, *args, **kwargs):
        """Not applicable for CNC."""
        pass
    
    def change_tool(self, tool, *args, **kwargs):
        """Tool change - handled by the stream engine for M6 during execution.
        For manual tool changes outside streaming, just log."""
        self._logger.info(f"Tool change requested: {tool}")
        # During streaming, M6 is intercepted by _stream_loop
        # Outside streaming, nothing to do (GRBL doesn't support M6 natively)
    
    def set_temperature(self, heater, value, tags=None, *args, **kwargs):
        """Not applicable for CNC."""
        pass
    
    def set_temperature_offset(self, offsets=None, tags=None, *args, **kwargs):
        """Not applicable for CNC."""
        pass
    
    @property
    def temperature_offsets(self) -> dict:
        """Not applicable for CNC."""
        return {}
    
    def feed_rate(self, factor, tags=None, *args, **kwargs):
        """Set feed rate override."""
        # GRBL uses real-time commands for override
        pass
    
    def flow_rate(self, factor, tags=None, *args, **kwargs):
        """Not applicable for CNC."""
        pass

    # -------------------------------------------------------------------------
    # Real-time Override Controls
    # -------------------------------------------------------------------------

    def send_override(self, override_type: str, action: str):
        """
        Send a real-time override command to GRBL.
        
        Args:
            override_type: 'feed', 'rapid', or 'spindle'
            action: action name, e.g. 'reset', 'plus10', 'minus10', 'plus1', 'minus1',
                    or for rapid: '100', '50', '25'
        """
        if self._comm is None:
            return False

        method_name = f"{override_type}_override_{action}"
        method = getattr(self._comm, method_name, None)
        if method is None:
            self._logger.warning(f"Unknown override: {method_name}")
            return False

        method()
        return True
