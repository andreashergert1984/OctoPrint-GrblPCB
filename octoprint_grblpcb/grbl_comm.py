# -*- coding: utf-8 -*-
"""
GRBL Communication Module

Low-level serial communication with GRBL controllers.
Handles GRBL-specific protocol including status parsing, error handling,
and real-time commands.
"""

import glob
import logging
import queue
import re
import threading
import time
from typing import Callable, Optional

import serial


# GRBL Error codes (GRBL 1.1)
GRBL_ERRORS = {
    1: "G-code words consist of a letter and a value. Letter was not found.",
    2: "Numeric value format is not valid or missing an expected value.",
    3: "Grbl '$' system command was not recognized or supported.",
    4: "Negative value received for an expected positive value.",
    5: "Homing cycle is not enabled via settings.",
    6: "Minimum step pulse time must be greater than 3usec.",
    7: "EEPROM read failed. Reset and restored to default values.",
    8: "Grbl '$' command cannot be used unless Grbl is IDLE.",
    9: "G-code locked out during alarm or jog state.",
    10: "Soft limits cannot be enabled without homing also enabled.",
    11: "Max characters per line exceeded. Line was not processed and executed.",
    12: "Grbl '$' setting value exceeds the maximum step rate supported.",
    13: "Safety door detected as opened and door state initiated.",
    14: "Build info or startup line exceeded EEPROM line length limit.",
    15: "Jog target exceeds machine travel. Command ignored.",
    16: "Jog command with no '=' or contains prohibited g-code.",
    17: "Laser mode requires PWM output.",
    20: "Unsupported or invalid g-code command found in block.",
    21: "More than one g-code command from same modal group found in block.",
    22: "Feed rate has not yet been set or is undefined.",
    23: "G-code command in block requires an integer value.",
    24: "Two G-code commands that both require the use of the XYZ axis words were detected in the block.",
    25: "A G-code word was repeated in the block.",
    26: "A G-code command implicitly or explicitly requires XYZ axis words in the block, but none were detected.",
    27: "N line number value is not within the valid range of 1 - 9,999,999.",
    28: "A G-code command was sent, but is missing some required P or L value words in the line.",
    29: "Grbl supports six work coordinate systems G54-G59. G59.1, G59.2, and G59.3 are not supported.",
    30: "The G53 G-code command requires either a G0 seek or G1 feed motion mode to be active.",
    31: "There are unused axis words in the block and G80 motion mode cancel is active.",
    32: "A G2 or G3 arc was commanded but there are no XYZ axis words in the selected plane to trace the arc.",
    33: "The motion command has an invalid target. G2, G3, and G38.2 generates this error.",
    34: "A G2 or G3 arc, traced with the radius definition, had a mathematical error when computing the arc geometry.",
    35: "A G2 or G3 arc, traced with the offset definition, is missing the IJK offset word in the selected plane.",
    36: "There are unused, leftover G-code words that aren't used by any command in the block.",
    37: "The G43.1 dynamic tool length offset command cannot apply an offset to an axis other than its configured axis.",
    38: "Tool number greater than max supported value.",
}

# GRBL Alarm codes (GRBL 1.1)
GRBL_ALARMS = {
    1: "Hard limit triggered. Position is likely lost.",
    2: "G-code motion target exceeds machine travel.",
    3: "Reset while in motion. Position is likely lost.",
    4: "Probe fail. Probe did not contact workpiece.",
    5: "Probe fail. Probe did not clear initial probe trigger.",
    6: "Homing fail. Reset during active homing cycle.",
    7: "Homing fail. Safety door was opened during homing cycle.",
    8: "Homing fail. Pull-off travel failed to clear limit switch.",
    9: "Homing fail. Could not find limit switch within search distance.",
    10: "Homing fail. Second dual axis limit switch failed to trigger.",
}


def grbl_serial_list() -> list[str]:
    """Return list of available serial ports suitable for GRBL."""
    ports = []
    
    # Linux
    ports.extend(glob.glob("/dev/ttyUSB*"))
    ports.extend(glob.glob("/dev/ttyACM*"))
    ports.extend(glob.glob("/dev/ttyCH341USB*"))
    
    # macOS
    ports.extend(glob.glob("/dev/cu.usbserial*"))
    ports.extend(glob.glob("/dev/cu.usbmodem*"))
    
    # Filter out common non-GRBL ports
    ports = [p for p in ports if not any(
        exclude in p for exclude in ["Bluetooth", "MALS", "SOC"]
    )]

    # Ports configured in OctoPrint under Serial Connection > Additional serial
    # ports (globs), e.g. virtual ports of the GRBL simulator
    for pattern in _additional_port_patterns():
        ports.extend(glob.glob(pattern))

    return sorted(set(ports))


def _additional_port_patterns() -> list[str]:
    """Return the additional serial port globs from OctoPrint's settings."""
    try:
        from octoprint.settings import settings
        return settings().get(["plugins", "serial_connector", "additionalPorts"]) or []
    except Exception:
        # Settings are not initialised (e.g. in unit tests)
        return []


class GrblComm:
    """
    Low-level GRBL serial communication handler.
    
    Handles:
    - Serial connection management
    - GRBL status parsing (<...> format)
    - Error and alarm handling
    - Real-time commands (!, ~, ?)
    - Response buffering
    """
    
    # Status report: <State|Field:values|...>, fields in any order
    STATUS_PATTERN = re.compile(r"<([^>]*)>")

    ERROR_PATTERN = re.compile(r"error:(\d+)")
    ALARM_PATTERN = re.compile(r"ALARM:(\d+)")
    
    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        status_callback: Optional[Callable[[dict], None]] = None,
        response_callback: Optional[Callable[[str], None]] = None,
        error_callback: Optional[Callable[[int, str], None]] = None,
        alarm_callback: Optional[Callable[[int, str], None]] = None,
    ):
        self._port = port
        self._baudrate = baudrate
        self._serial: Optional[serial.Serial] = None
        
        self._status_callback = status_callback
        self._response_callback = response_callback
        self._error_callback = error_callback
        self._alarm_callback = alarm_callback
        
        self._lock = threading.Lock()
        self._logger = logging.getLogger(__name__)
        
        # Last known work coordinate offset
        self._last_wco = {"x": 0.0, "y": 0.0, "z": 0.0}
        
        # Response queue for streaming mode
        self._response_queue = queue.Queue()
        # When True, the reader puts 'ok' and 'error' into the queue
        self._stream_mode = False

        # Background reader: handles every response as soon as it arrives, so
        # the streaming loop is never throttled by a polling interval
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_stop = threading.Event()
    
    def connect(self):
        """Open serial connection to GRBL controller."""
        with self._lock:
            if self._serial is not None:
                return
            
            self._logger.info(f"Opening serial port {self._port} @ {self._baudrate}")
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                timeout=0.1,  # Non-blocking reads
                write_timeout=1.0,
            )
            
            # Wait for GRBL to wake up
            time.sleep(0.5)
            
            # Flush input buffer
            self._serial.reset_input_buffer()

            self._reader_stop.clear()
            self._reader_thread = threading.Thread(
                target=self._read_loop, args=(self._serial,), daemon=True
            )
            self._reader_thread.start()
    
    def disconnect(self):
        """Close serial connection."""
        self._reader_stop.set()
        with self._lock:
            if self._serial is not None:
                self._serial.close()
                self._serial = None
                self._logger.info("Serial port closed")
        if self._reader_thread is not None and self._reader_thread is not threading.current_thread():
            self._reader_thread.join(timeout=1.0)
        self._reader_thread = None
    
    def is_connected(self) -> bool:
        """Check if serial connection is open."""
        return self._serial is not None and self._serial.is_open
    
    def send_command(self, command: str):
        """
        Send a G-code command to GRBL.
        
        The command is automatically terminated with a newline.
        """
        with self._lock:
            if self._serial is None:
                return
            
            # Strip and add newline
            command = command.strip()
            
            self._logger.debug(f"Sending: {command}")
            self._serial.write(f"{command}\n".encode("utf-8"))
    
    def request_status(self):
        """Send status request (?)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"?")
    
    def feed_hold(self):
        """Send feed hold (!)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"!")
    
    def cycle_resume(self):
        """Send cycle resume (~)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"~")
    
    def soft_reset(self):
        """Send soft reset (Ctrl-X)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"\x18")
    
    def jog_cancel(self):
        """Cancel jog (0x85)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"\x85")

    # ----- Real-time override commands (GRBL 1.1) -----

    def feed_override_reset(self):
        """Reset feed override to 100% (0x90)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"\x90")

    def feed_override_plus10(self):
        """Increase feed override +10% (0x91)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"\x91")

    def feed_override_minus10(self):
        """Decrease feed override -10% (0x92)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"\x92")

    def feed_override_plus1(self):
        """Increase feed override +1% (0x93)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"\x93")

    def feed_override_minus1(self):
        """Decrease feed override -1% (0x94)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"\x94")

    def rapid_override_100(self):
        """Set rapid override to 100% (0x95)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"\x95")

    def rapid_override_50(self):
        """Set rapid override to 50% (0x96)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"\x96")

    def rapid_override_25(self):
        """Set rapid override to 25% (0x97)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"\x97")

    def spindle_override_reset(self):
        """Reset spindle override to 100% (0x99)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"\x99")

    def spindle_override_plus10(self):
        """Increase spindle override +10% (0x9A)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"\x9A")

    def spindle_override_minus10(self):
        """Decrease spindle override -10% (0x9B)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"\x9B")

    def spindle_override_plus1(self):
        """Increase spindle override +1% (0x9C)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"\x9C")

    def spindle_override_minus1(self):
        """Decrease spindle override -1% (0x9D)."""
        with self._lock:
            if self._serial is not None:
                self._serial.write(b"\x9D")
    
    def _read_loop(self, port):
        """Read and dispatch GRBL responses until disconnected."""
        buffer = b""
        while not self._reader_stop.is_set():
            try:
                # readline() would hand out partial lines on timeout
                buffer += port.read(port.in_waiting or 1)
            except (serial.SerialException, OSError, TypeError) as e:
                # TypeError: pyserial on a port closed by disconnect()
                if not self._reader_stop.is_set():
                    self._logger.warning(f"Serial read failed: {e}")
                break
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    self._process_line(line)
                except Exception:
                    self._logger.exception(f"Error while handling GRBL response {line!r}")
    
    def _process_line(self, line: str):
        """Process a single line from GRBL."""
        self._logger.debug(f"Received: {line}")
        
        # Check for status report
        if line.startswith("<"):
            self._parse_status(line)
            return
        
        # Check for error
        error_match = self.ERROR_PATTERN.match(line)
        if error_match:
            error_code = int(error_match.group(1))
            error_msg = GRBL_ERRORS.get(error_code, "Unknown error")
            self._logger.warning(f"GRBL error {error_code}: {error_msg}")
            if self._stream_mode:
                self._response_queue.put(("error", error_code, error_msg))
            if self._error_callback:
                self._error_callback(error_code, error_msg)
            return
        
        # Check for alarm
        alarm_match = self.ALARM_PATTERN.match(line)
        if alarm_match:
            alarm_code = int(alarm_match.group(1))
            alarm_msg = GRBL_ALARMS.get(alarm_code, "Unknown alarm")
            self._logger.error(f"GRBL ALARM {alarm_code}: {alarm_msg}")
            if self._stream_mode:
                self._response_queue.put(("alarm", alarm_code, alarm_msg))
            if self._alarm_callback:
                self._alarm_callback(alarm_code, alarm_msg)
            return
        
        # Check for 'ok' response
        if line == "ok":
            if self._stream_mode:
                self._response_queue.put(("ok",))
            if self._response_callback:
                self._response_callback(line)
            return
        
        # Check for probe result [PRB:x,y,z:success]
        prb_match = re.match(r"\[PRB:([+-]?\d+\.?\d*),([+-]?\d+\.?\d*),([+-]?\d+\.?\d*):(\d)\]", line)
        if prb_match:
            if self._stream_mode:
                self._response_queue.put(("probe", {
                    "x": float(prb_match.group(1)),
                    "y": float(prb_match.group(2)),
                    "z": float(prb_match.group(3)),
                    "success": prb_match.group(4) == "1",
                }))
        
        # Pass other responses through
        if self._response_callback:
            self._response_callback(line)
    
    def _parse_status(self, line: str):
        """
        Parse GRBL status report.
        
        Format: <State|MPos:x,y,z|FS:feed,spindle|WCO:x,y,z|Ov:feed,rapid,spindle>
        """
        match = self.STATUS_PATTERN.search(line)
        if not match:
            self._logger.debug(f"Could not parse status: {line}")
            return

        state, *fields = match.group(1).split("|")
        status = {
            "state": state,
            "raw": line,
        }

        for field in fields:
            name, _, value = field.partition(":")
            try:
                if name in ("MPos", "WPos", "WCO"):
                    x, y, z = (float(v) for v in value.split(",")[:3])
                    key = {"MPos": "mpos", "WPos": "wpos", "WCO": "wco"}[name]
                    status[key] = {"x": x, "y": y, "z": z}
                elif name in ("FS", "F"):
                    values = [int(float(v)) for v in value.split(",")]
                    status["fs"] = {
                        "feed": values[0],
                        "spindle": values[1] if len(values) > 1 else 0,
                    }
                elif name == "Ov":
                    feed, rapid, spindle = (int(v) for v in value.split(",")[:3])
                    status["overrides"] = {"feed": feed, "rapid": rapid, "spindle": spindle}
                elif name == "Pn":
                    status["pins"] = value
            except ValueError:
                self._logger.debug(f"Could not parse status field {field!r} in {line}")

        # GRBL sends WCO only every few reports; remember it for the others
        if "wco" in status:
            self._last_wco = status["wco"]
        else:
            status["wco"] = self._last_wco

        if self._status_callback:
            self._status_callback(status)
