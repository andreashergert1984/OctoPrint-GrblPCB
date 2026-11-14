# -*- coding: utf-8 -*-
"""
GRBL Connector for OctoPrint

This module provides a ConnectedPrinter implementation for GRBL-based CNC machines.
It registers as a separate connector type in OctoPrint's connection system.
"""

import logging
import queue
import re
import threading
import time
from gettext import gettext
from typing import Any

from octoprint.printer import (
    ConnectedPrinterCapabilities,
    ErrorInformation,
    FirmwareInformation,
)
from octoprint.printer.connection import (
    ConnectedPrinter,
    ConnectedPrinterState,
)
from octoprint.printer.job import PrintJob

from .grbl_comm import GrblComm, grbl_serial_list


class _StreamAbort(Exception):
    """Ends a running job: stopped by the user or failed for ``reason``."""

    def __init__(self, reason: str, **info):
        super().__init__(reason)
        self.reason = reason
        self.info = dict(info, reason=reason)


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
    
    # Defaults for state that __init__ sets per instance
    _status_seq = 0
    _link_lost = False
    _stream_error_count = 0
    _stream_failure = None
    _stream_notes: dict = {}

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
        self._paused_for_intervention = threading.Event()
        self._stream_notes = {}
        self._stream_error_count = 0
        self._stream_failure = None
        # (line index, bytes) of lines sent but not yet answered by GRBL
        self._stream_pending = []
        # Held while a line is sent, so a stop never races with one more line
        self._stream_send_lock = threading.Lock()
        # Counts status reports: lets waiters tell a fresh report from an old one
        self._status_seq = 0
        self._link_lost = False
        
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
                disconnect_callback=self._on_connection_lost,
            )
            self._link_lost = False
            self._comm.connect()
            
            # Wait for GRBL to wake up and send banner
            time.sleep(self.WAKE_UP_DELAY)
            
            # Send soft reset and get status
            self._comm.soft_reset()
            time.sleep(0.5)
            self._await_first_status()
            # Travel and homing settings for the pre-flight check
            self._comm.send_command("$$")
            
            # Set firmware info
            self.firmware_info = FirmwareInformation(
                name="GRBL",
                data={
                    "FIRMWARE_NAME": self._comm.banner or "GRBL",
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
            comm, self._comm = self._comm, None
            if comm is not None:
                # Do not keep the port open: the next attempt needs it
                try:
                    comm.disconnect()
                except Exception:
                    self._logger.debug("Closing the port after a failed connect failed", exc_info=True)
            raise

    WAKE_UP_DELAY = 2.0  # seconds; boards with a bootloader reset on connect
    FIRST_STATUS_TIMEOUT = 3.0

    def _await_first_status(self):
        """
        Make sure a GRBL controller answers on this port.

        Without this check a wrong port or baud rate looked "Operational"
        and every command went nowhere.
        """
        seq = self._status_seq
        deadline = time.monotonic() + self.FIRST_STATUS_TIMEOUT
        while time.monotonic() < deadline:
            self._comm.request_status()
            time.sleep(0.1)
            if self._status_seq > seq:
                return
        raise ConnectionError(gettext(
            "No GRBL status report on %(port)s at %(baudrate)s baud. Check the port and the baud rate."
        ) % {"port": self._port, "baudrate": self._baudrate})

    def _on_connection_lost(self, reason: str):
        """The serial port died (cable pulled, controller reset its USB)."""
        self._logger.error(f"Connection to GRBL lost: {reason}")
        self._link_lost = True
        self._stream_stop.set()
        self._tool_change_event.set()
        self._stop_polling.set()
        comm, self._comm = self._comm, None
        if comm is not None:
            try:
                comm.disconnect()
            except Exception:
                self._logger.debug("Closing the lost port failed", exc_info=True)
        self._z_zeroed = False
        self._grbl_state = "Unknown"
        self.error_info = ErrorInformation(
            error=gettext("Connection to GRBL lost"),
            reason=reason,
            consequence=gettext("The position and the work zero may be lost. Reconnect, home or check the zero before milling."),
        )
        self.state = ConnectedPrinterState.CLOSED_WITH_ERROR
        if self._plugin_manager:
            self._plugin_manager.send_plugin_message(
                "grblpcb", {"type": "connection_lost", "message": reason}
            )
    
    def disconnect(self, *args, **kwargs):
        """Disconnect from GRBL controller."""
        self._logger.info("Disconnecting from GRBL")

        # A running job must not outlive the connection
        if self.is_streaming():
            self.stop_streaming()
            self._stream_thread.join(timeout=2.0)

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
        self._status_seq += 1
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
                    "z_zeroed": self._z_zeroed,
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
    
    def travel_limits(self):
        """
        Machine travel from GRBL's settings, or None if they are unknown.

        ``homing`` tells whether machine coordinates mean anything: without
        a homing cycle GRBL counts from wherever it was switched on.
        """
        settings = self._comm.settings if self._comm is not None else {}
        try:
            travel = {axis: float(settings[number]) for axis, number in (("x", 130), ("y", 131), ("z", 132))}
        except (KeyError, ValueError):
            return None
        return {
            "travel": travel,
            "homing": settings.get(22, "0") not in ("0", ""),
            "soft_limits": settings.get(20, "0") not in ("0", ""),
        }

    def _reject_during_job(self, what: str) -> bool:
        """
        True (and a log line) if a manual command must not go out now.

        OctoPrint's terminal and control tab end up here as well. A line
        mixed into a running job moves the machine in the middle of a cut
        and breaks the character counting of the stream.
        """
        if self.accepts_manual_commands():
            return False
        self._listener.on_printer_logs(
            f"!!! {what} " + gettext("not sent: a job is running. Wait for a pause or stop the job.")
        )
        return True

    def commands(self, *commands, tags=None, force=False, **kwargs):
        """Send G-code commands to GRBL."""
        if self._comm is None:
            return
        
        if len(commands) == 1 and isinstance(commands[0], (list, tuple)):
            commands = commands[0]

        if self._reject_during_job(", ".join(str(c) for c in commands)):
            return
        
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
        if self._reject_during_job(jog_cmd):
            return
        
        self._listener.on_printer_logs(f">>> {jog_cmd}")
        self._comm.send_command(jog_cmd)
    
    def home(self, axes=None, *args, **kwargs):
        """Home the CNC machine."""
        if self._comm is None:
            return
        
        # GRBL homes all axes with $H
        if self._reject_during_job("$H"):
            return
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
        
        # Parse lines; keep the comment of pause lines (e.g. which drill)
        lines = []
        self._stream_notes = {}
        for line in gcode_text.splitlines():
            line = line.strip()
            comment = ""
            if ";" in line:
                comment = line[line.index(";") + 1:].strip()
                line = line[:line.index(";")].strip()
            if line.startswith("(") and line.endswith(")"):
                continue
            if not line:
                continue
            if comment and self._pause_kind(line):
                self._stream_notes[len(lines)] = comment
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
    
    HOLD_TIMEOUT = 3.0  # seconds to wait for the machine to stand still

    def stop_streaming(self):
        """Stop the current G-code stream and the machine."""
        with self._stream_send_lock:
            self._stream_stop.set()
        self._tool_change_event.set()  # unblock any waiting tool change
        self._halt_machine()

    def _halt_machine(self):
        """
        Feed hold, wait until the axes stand still, then soft reset.

        A reset while the axes still move makes GRBL drop the step pulses:
        ALARM:3, the position is lost and the board has to be zeroed again.
        After a completed hold the reset keeps the position.
        """
        comm = self._comm
        if comm is None:
            return
        try:
            seq = self._status_seq
            comm.feed_hold()
            deadline = time.monotonic() + self.HOLD_TIMEOUT
            while time.monotonic() < deadline:
                comm.request_status()
                time.sleep(0.05)
                # Two reports later the state certainly reflects the hold
                if self._status_seq >= seq + 2 and self._grbl_state in ("Hold:0", "Idle", "Alarm", "Door:0"):
                    break
            else:
                self._logger.warning("Machine did not confirm the feed hold in time; resetting anyway")
            comm.soft_reset()
        except OSError as e:
            self._logger.error(f"Could not halt the machine: {e}")

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
        """
        Tool up and spindle off before the user works on the machine.

        Returns only when GRBL reports Idle: the user is asked to change
        the tool once nothing moves any more.
        """
        safe_z = 5.0
        z_feed = 120.0
        if self._plugin_settings is not None:
            try:
                safe_z = float(self._plugin_settings.get_float(["safe_z_height"]))
                z_feed = float(self._plugin_settings.get_float(["z_max_feed"]))
            except (TypeError, ValueError, AttributeError):
                pass

        # Force absolute mode so the retract move cannot be interpreted relative.
        for command in ("G90", f"G1 Z{safe_z:.3f} F{z_feed:.0f}", "M5"):
            self._send_counted(command, None)
        self._drain()
        self._wait_idle()

    # ----- sending with character counting --------------------------------

    RX_BUFFER_SIZE = 127  # GRBL's serial RX buffer holds 128 bytes
    LINK_TIMEOUT = 5.0  # seconds without a byte from GRBL (status is polled 4x/s)

    def _stream_comm(self) -> GrblComm:
        """The connection, or abort the job if it is gone."""
        comm = self._comm
        if comm is None or self._link_lost:
            raise _StreamAbort("connection_lost")
        return comm

    def _check_stop(self):
        if self._stream_stop.is_set():
            raise _StreamAbort("connection_lost" if self._link_lost else "stopped")

    def _take_response(self, resp):
        """Account one answer of GRBL; errors and alarms end the job."""
        kind = resp[0]
        if kind == "ok":
            if self._stream_pending:
                self._stream_pending.pop(0)
        elif kind == "error":
            line_index = self._stream_pending.pop(0)[0] if self._stream_pending else None
            self._stream_error_count += 1
            self._logger.error(f"GRBL error {resp[1]} ({resp[2]}) at line index {line_index}")
            raise _StreamAbort("grbl_error", code=resp[1], message=resp[2], line_index=line_index)
        elif kind == "alarm":
            self._logger.error(f"ALARM during stream: {resp[2]}")
            raise _StreamAbort("alarm", code=resp[1], message=resp[2])

    def _pump(self, timeout: float = 0.0) -> bool:
        """
        Handle the answers GRBL sent: all that are waiting; with ``timeout``
        wait that long for the first one. Returns False if none came.
        """
        responses = self._stream_comm()._response_queue
        try:
            resp = responses.get(timeout=timeout) if timeout > 0 else responses.get_nowait()
        except queue.Empty:
            if timeout > 0 and self._stream_comm().seconds_since_rx() > self.LINK_TIMEOUT:
                raise _StreamAbort("connection_lost") from None
            return False
        while True:
            self._take_response(resp)
            try:
                resp = responses.get_nowait()
            except queue.Empty:
                return True

    def _send_counted(self, line: str, line_index):
        """Send one line as soon as GRBL's RX buffer has room for it."""
        cmd_len = len(line) + 1  # +1 for newline
        # Look at the answers before every line: an error or alarm is
        # noticed at once, not only when the buffer is full
        self._pump()
        while self._stream_pending and (
            sum(length for _, length in self._stream_pending) + cmd_len > self.RX_BUFFER_SIZE
        ):
            self._check_stop()
            self._pump(timeout=0.25)
        with self._stream_send_lock:
            self._check_stop()
            self._stream_comm().send_command(line)
        self._stream_pending.append((line_index, cmd_len))

    def _drain(self):
        """Wait until GRBL answered every line sent so far."""
        while self._stream_pending:
            self._check_stop()
            self._pump(timeout=0.25)

    def _wait_idle(self):
        """
        Wait until the machine has finished moving.

        "ok" only means GRBL took a line into its planner; the motion comes
        later. Wait for two fresh status reports so an old "Idle" from
        before the last line does not count.
        """
        seq = self._status_seq
        while True:
            self._check_stop()
            self._pump(timeout=0.1)
            if self._grbl_state == "Alarm":
                raise _StreamAbort("alarm")
            if self._status_seq >= seq + 2 and self._grbl_state == "Idle":
                return

    def _stream_loop(self, operation_name: str):
        """
        Background thread: stream G-code using GRBL character-counting protocol.

        GRBL has a 128-byte serial RX buffer. We track how many bytes we've sent
        and wait for 'ok' responses to free buffer space before sending more.

        The job ends as "completed" (every line done, machine idle),
        "stopped" (by the user) or "failed": a GRBL error or alarm, or a lost
        connection. After an error the machine is halted, because the
        rejected line may have been the retract before a travel move.
        """
        comm = self._comm
        comm.begin_capture()
        self._stream_pending = []
        self._stream_error_count = 0
        self._stream_failure = None

        self.state = ConnectedPrinterState.PRINTING
        self._send_stream_progress("started", operation_name)

        outcome = "completed"
        try:
            for i, line in enumerate(self._stream_lines):
                # Handle pause
                while self._stream_pause.is_set() and not self._stream_stop.is_set():
                    time.sleep(0.1)
                self._check_stop()

                # M6 tool change and M0 program pause: neither goes to GRBL
                # (it rejects M6); the user works on the machine and confirms
                pause_status = self._pause_kind(line)
                if pause_status:
                    self._drain()
                    self._safe_pause_before_manual_intervention()
                    if not self._pause_for_intervention(pause_status, operation_name, i):
                        self._check_stop()
                        raise _StreamAbort("stopped")
                    self._stream_current_line = i + 1
                    continue

                self._send_counted(line, i)
                self._stream_current_line = i + 1

                # Send progress every 10 lines or on last line
                if (i + 1) % 10 == 0 or i == self._stream_total_lines - 1:
                    self._send_stream_progress("progress", operation_name, line_num=i + 1)

            self._drain()
            self._wait_idle()

        except _StreamAbort as abort:
            if abort.reason == "stopped":
                outcome = "stopped"
            else:
                outcome = "failed"
                self._stream_failure = abort.info
                if abort.reason == "grbl_error":
                    self._halt_machine()
        except Exception as e:
            self._logger.exception("Stream error")
            outcome = "failed"
            lost = isinstance(e, OSError) or self._link_lost
            self._stream_failure = {
                "reason": "connection_lost" if lost else "internal_error",
                "message": str(e),
            }
        finally:
            comm.end_capture()
            if self._comm is not None and not self._link_lost:
                self.state = ConnectedPrinterState.OPERATIONAL
            self._send_stream_progress(outcome, operation_name, line_num=self._stream_current_line)

    SETTLE_QUIET = 0.5  # seconds without responses before streaming resumes

    @staticmethod
    def _pause_kind(line: str):
        """"tool_change" for M6 (also "T2 M6"), "pause" for M0, else None."""
        if re.search(r"\bM0?6\b", line, re.IGNORECASE):
            return "tool_change"
        if re.search(r"\bM0{1,2}\b", line, re.IGNORECASE):
            return "pause"
        return None

    def is_paused_for_intervention(self) -> bool:
        """True while a job waits at M0/M6 for the user."""
        return self._paused_for_intervention.is_set()

    def accepts_manual_commands(self) -> bool:
        """Manual G-code is allowed without a job or while it is paused."""
        return not self.is_streaming() or self.is_paused_for_intervention()

    def _pause_for_intervention(self, status: str, operation_name: str, line_num: int) -> bool:
        """
        Wait for the user's confirmation and for the machine to settle.

        Returns False if the job was stopped. An alarm during the pause
        voids a confirmation already given: after unlocking, the user has
        to check the machine and confirm again.
        """
        self._paused_for_intervention.set()
        try:
            self._send_stream_progress(status, operation_name, line_num=line_num)
            self._tool_change_event.clear()
            while True:
                while not self._tool_change_event.is_set() and not self._stream_stop.is_set():
                    time.sleep(0.1)
                if self._stream_stop.is_set():
                    return False
                if self._settle_after_pause():
                    return True
                if self._stream_stop.is_set():
                    return False
                self._tool_change_event.clear()
                self._send_stream_progress("confirm_again", operation_name, line_num=line_num)
        finally:
            self._paused_for_intervention.clear()

    def _settle_after_pause(self) -> bool:
        """
        Wait until manual commands sent during the pause are done.

        During a tool change the user jogs, probes and zeroes; GRBL answers
        those with ok/[PRB] into the stream queue. Counting them as answers
        to streamed lines would overfill GRBL's 128-byte RX buffer. Discard
        them until GRBL is idle and nothing arrives for SETTLE_QUIET.

        Returns True once settled, False if GRBL is in alarm (or the job
        was stopped).
        """
        started = quiet_since = time.time()
        reported = False
        while not self._stream_stop.is_set():
            if self._grbl_state == "Alarm":
                return False
            try:
                self._stream_comm()._response_queue.get(timeout=0.1)
                quiet_since = time.time()
                continue
            except queue.Empty:
                pass
            if self._grbl_state == "Idle" and time.time() - quiet_since >= self.SETTLE_QUIET:
                return True
            if not reported and time.time() - started > 2.0:
                # e.g. a probe move is still running: say why we wait
                self._send_stream_progress("waiting_for_idle", "", line_num=self._stream_current_line)
                reported = True

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
                    "errors": self._stream_error_count,
                    "note": self._stream_notes.get(line_num, ""),
                    "command": self._stream_lines[line_num] if status in ("tool_change", "pause") and line_num < len(self._stream_lines) else "",
                    **self._failure_details(status),
                }
            )

    def _failure_details(self, status: str) -> dict:
        """What went wrong, for the "failed" message to the frontend."""
        if status != "failed" or not self._stream_failure:
            return {}
        failure = self._stream_failure
        details = {
            "reason": failure.get("reason", ""),
            "error_code": failure.get("code"),
            "error_message": failure.get("message", ""),
        }
        line_index = failure.get("line_index")
        if line_index is not None and line_index < len(self._stream_lines):
            details["failed_line"] = line_index + 1
            details["failed_command"] = self._stream_lines[line_index]
        return details
    
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
