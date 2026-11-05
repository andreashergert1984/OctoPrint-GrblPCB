#!/usr/bin/env python3
"""
GRBL 1.1 simulator for testing OctoPrint-GrblPCB without hardware.

Creates a pseudo terminal and links its device to the given path, so OctoPrint
can connect to it like to a USB serial port:

    python tools/grbl_simulator.py /tmp/ttyGRBL

Add the path (or a glob matching it) to OctoPrint's
``serial.additionalPorts`` setting so the plugin lists it.

Behaviour modelled after GRBL 1.1:
- Real-time bytes (``?``, ``!``, ``~``, Ctrl-X, override bytes) act
  immediately and are not line based; they never answer with ``ok``.
- Lines are answered with ``ok`` once they are queued in a planner buffer of
  15 blocks; motion runs in the background, the status report shows ``Run``.
- ``G38.2``/``G38.3`` probe against a slightly tilted surface and send a
  ``[PRB:...]`` report, so autolevel produces a real height map.
- With ``--homing`` the machine starts in ``Alarm`` until ``$H`` or ``$X``.
"""

import argparse
import math
import os
import queue
import re
import select
import sys
import threading
import time
import tty

VERSION_BANNER = "Grbl 1.1h ['$' for help]"
PLANNER_BLOCKS = 15
AXES = ("x", "y", "z")
WCS_CODES = ("G54", "G55", "G56", "G57", "G58", "G59")
WORD_RE = re.compile(r"([A-Z])\s*([+-]?\d*\.?\d+)")

SETTINGS = {
    0: "10", 1: "25", 2: "0", 3: "0", 4: "0", 5: "0", 6: "0", 10: "1",
    11: "0.010", 12: "0.002", 13: "0", 20: "0", 21: "0", 22: "1", 23: "0",
    24: "25.000", 25: "500.000", 26: "250", 27: "1.000", 30: "10000",
    31: "0", 32: "0", 100: "250.000", 101: "250.000", 102: "250.000",
    110: "3000.000", 111: "3000.000", 112: "600.000", 120: "200.000",
    121: "200.000", 122: "200.000", 130: "200.000", 131: "200.000",
    132: "50.000",
}


class GrblSimulator:
    """State machine of a simulated GRBL 1.1 controller."""

    def __init__(self, write, homing=False, time_scale=0.05, surface_z=-1.0):
        self._write = write
        self._lock = threading.RLock()
        self._planner = queue.Queue(maxsize=PLANNER_BLOCKS)
        self._time_scale = time_scale
        self._homing = homing
        self._surface_z = surface_z
        self._reset_state()
        threading.Thread(target=self._motion_loop, daemon=True).start()

    # -- state -------------------------------------------------------------

    def _reset_state(self):
        self.state = "Alarm" if self._homing else "Idle"
        self.mpos = {a: 0.0 for a in AXES}
        if not hasattr(self, "offsets"):
            # Work offsets live in EEPROM and survive a soft reset
            self.offsets = {w: {a: 0.0 for a in AXES} for w in WCS_CODES}
        self.wcs = "G54"
        self._reports = 0
        self.motion = "G0"
        self.absolute = True
        self.inches = False
        self.feed = 0.0
        self.spindle = 0.0
        self.spindle_mode = "M5"
        self.probe = None
        self._drain_planner()
        self._planned_end = dict(self.mpos)

    def _drain_planner(self):
        while True:
            try:
                self._planner.get_nowait()
                self._planner.task_done()
            except queue.Empty:
                return

    def wpos(self):
        off = self.offsets[self.wcs]
        return {a: self.mpos[a] - off[a] for a in AXES}

    def surface_at(self, x, y):
        """Copper surface height in machine coordinates (slightly warped)."""
        return self._surface_z + 0.002 * x - 0.001 * y + 0.03 * math.sin(x / 15.0)

    def send(self, line):
        self._write(f"{line}\r\n".encode())

    # -- input -------------------------------------------------------------

    def realtime(self, byte):
        """Handle a real-time byte. Returns True if it was one."""
        with self._lock:
            if byte == 0x3F:  # ?
                self.send(self._status())
            elif byte == 0x21:  # !
                if self.state in ("Run", "Jog"):
                    self.state = "Hold:0"
            elif byte == 0x7E:  # ~
                if self.state.startswith("Hold"):
                    self.state = "Run"
            elif byte == 0x18:  # Ctrl-X
                # Reset during motion loses steps: GRBL raises ALARM:3
                in_motion = self.state in ("Run", "Jog", "Home") or self.state.startswith("Hold")
                position = dict(self.mpos)
                self._reset_state()
                if in_motion:
                    self.mpos = position
                    self._planned_end = dict(position)
                    self.state = "Alarm"
                    self.send("ALARM:3")
                self.send("")
                self.send(VERSION_BANNER)
                if self.state == "Alarm":
                    self.send("[MSG:'$H'|'$X' to unlock]")
            elif byte == 0x85 or 0x90 <= byte <= 0xA1:
                if byte == 0x85 and self.state == "Jog":
                    self._drain_planner()
                    self._planned_end = dict(self.mpos)
                    self.state = "Idle"
            else:
                return False
            return True

    def line(self, raw):
        line = re.sub(r"\(.*?\)|;.*$", "", raw).strip().upper().replace(" ", "")
        if not line:
            return
        try:
            if line.startswith("$"):
                self._system(line)
            else:
                self._gcode(line, jog=False)
        except GrblError as err:
            self.send(f"error:{err.code}")

    # -- system commands -----------------------------------------------------

    def _system(self, line):
        if line == "$":
            self.send("[HLP:$$ $# $G $I $N $x=val $Nx=line $J=line $SLP $C $X $H ~ ! ? ctrl-x]")
        elif line == "$$":
            for key, val in SETTINGS.items():
                self.send(f"${key}={val}")
        elif line == "$#":
            for idx, w in enumerate(WCS_CODES):
                o = self.offsets[w]
                self.send(f"[{w}:{o['x']:.3f},{o['y']:.3f},{o['z']:.3f}]")
            self.send("[G28:0.000,0.000,0.000]")
            self.send("[G30:0.000,0.000,0.000]")
            self.send("[G92:0.000,0.000,0.000]")
            self.send("[TLO:0.000]")
            p = self.probe or (0.0, 0.0, 0.0, 0)
            self.send(f"[PRB:{p[0]:.3f},{p[1]:.3f},{p[2]:.3f}:{p[3]}]")
        elif line == "$G":
            self.send(self._parser_state())
        elif line == "$I":
            self.send("[VER:1.1h.20190825:SIM]")
            self.send("[OPT:V,15,128]")
        elif line == "$X":
            if self.state == "Alarm":
                self.state = "Idle"
            self.send("[MSG:Caution: Unlocked]")
        elif line == "$H":
            self._planner.join()
            with self._lock:
                self.state = "Home"
            time.sleep(1.0 * self._time_scale * 10)
            with self._lock:
                self.mpos = {a: 0.0 for a in AXES}
                self._planned_end = dict(self.mpos)
                self.state = "Idle"
        elif line.startswith("$J="):
            if self.state not in ("Idle", "Jog"):
                raise GrblError(8)
            self._gcode(line[3:], jog=True)
            return
        elif re.fullmatch(r"\$\d+=.*", line):
            key, val = line[1:].split("=", 1)
            SETTINGS[int(key)] = val
        else:
            raise GrblError(3)
        self.send("ok")

    # -- g-code --------------------------------------------------------------

    def _gcode(self, line, jog):
        if self.state == "Alarm":
            raise GrblError(9)
        words = WORD_RE.findall(line)
        if not words or "".join(l + v for l, v in words) != line:
            raise GrblError(1)
        if jog:
            modal = (self.absolute, self.inches, self.feed, self.motion)
            try:
                self.motion = "G1"
                self._run_words(words, jog=True)
            finally:
                self.absolute, self.inches, self.feed, self.motion = modal
        else:
            self._run_words(words, jog=False)

    def _run_words(self, words, jog):

        target = {}
        g10 = None
        probe = None
        for letter, value in words:
            num = float(value)
            if letter == "G":
                code = f"G{int(num)}" if num.is_integer() else f"G{num:g}"
                if code in ("G0", "G1", "G2", "G3"):
                    self.motion = code
                elif code in ("G38.2", "G38.3"):
                    probe = code
                elif code == "G90":
                    self.absolute = True
                elif code == "G91":
                    self.absolute = False
                elif code == "G20":
                    self.inches = True
                elif code == "G21":
                    self.inches = False
                elif code in WCS_CODES:
                    self.wcs = code
                    self._reports = 0
                elif code == "G10":
                    g10 = {}
                elif code in ("G4", "G17", "G94", "G53", "G40", "G49", "G80"):
                    pass
                else:
                    raise GrblError(20)
            elif letter == "M":
                code = int(num)
                if code in (3, 4):
                    self.spindle_mode = f"M{code}"
                elif code == 5:
                    self.spindle_mode = "M5"
                elif code in (0, 1, 2, 30, 8, 9):
                    pass
                else:
                    raise GrblError(20)
            elif letter == "F":
                self.feed = num
            elif letter == "S":
                self.spindle = num
            elif letter in ("X", "Y", "Z"):
                target[letter.lower()] = num * (25.4 if self.inches else 1.0)
            elif letter in ("L", "P", "T", "N", "I", "J", "R"):
                if g10 is not None:
                    g10[letter] = num
            else:
                raise GrblError(20)

        if g10 is not None:
            self._set_offset(g10, target)
            self.send("ok")
            return

        if probe:
            self._probe(target, fail_ok=(probe == "G38.3"))
            self.send("ok")
            return

        if target:
            end = self._resolve(target)
            feed = self.feed if (self.motion != "G0" or jog) else 3000.0
            if self.motion != "G0" and feed <= 0 and not jog:
                raise GrblError(22)
            self._planner.put((end, feed, jog))
        self.send("ok")

    def _resolve(self, target):
        """Target in machine coordinates; relative moves start at the planned end."""
        end = dict(self._planned_end)
        off = self.offsets[self.wcs]
        for a, v in target.items():
            end[a] = v + off[a] if self.absolute else end[a] + v
        self._planned_end = dict(end)
        return end

    def _set_offset(self, words, target):
        if int(words.get("L", 0)) not in (2, 20):
            raise GrblError(20)
        p = int(words.get("P", 0))
        wcs = self.wcs if p == 0 else WCS_CODES[p - 1]
        self._reports = 0
        for a, v in target.items():
            if int(words["L"]) == 20:
                self.offsets[wcs][a] = self.mpos[a] - v
            else:
                self.offsets[wcs][a] = v

    def _probe(self, target, fail_ok):
        self._planner.join()
        if "z" not in target:
            raise GrblError(37)
        if self.absolute:
            z_target = target["z"] + self.offsets[self.wcs]["z"]
        else:
            z_target = self.mpos["z"] + target["z"]
        with self._lock:
            self.state = "Run"
        surface = self.surface_at(self.mpos["x"], self.mpos["y"])
        hit = z_target <= surface <= self.mpos["z"]
        time.sleep(abs(self.mpos["z"] - (surface if hit else z_target)) / max(self.feed, 1) * 60 * self._time_scale)
        with self._lock:
            self.mpos["z"] = surface if hit else z_target
            self._planned_end = dict(self.mpos)
            self.probe = (self.mpos["x"], self.mpos["y"], self.mpos["z"], 1 if hit else 0)
            self.state = "Idle"
            self.send(f"[PRB:{self.mpos['x']:.3f},{self.mpos['y']:.3f},{self.mpos['z']:.3f}:{1 if hit else 0}]")
            if not hit and not fail_ok:
                self.state = "Alarm"
                self.send("ALARM:5")

    # -- motion --------------------------------------------------------------

    def _motion_loop(self):
        while True:
            end, feed, jog = self._planner.get()
            with self._lock:
                if self.state in ("Idle", "Run", "Jog"):
                    self.state = "Jog" if jog else "Run"
                start = dict(self.mpos)
            dist = math.sqrt(sum((end[a] - start[a]) ** 2 for a in AXES))
            duration = dist / max(feed, 1.0) * 60.0 * self._time_scale
            steps = max(1, int(duration / 0.02))
            for i in range(1, steps + 1):
                while self.state.startswith("Hold"):
                    time.sleep(0.02)
                if self.state in ("Alarm", "Idle") and i > 1:
                    break
                with self._lock:
                    for a in AXES:
                        self.mpos[a] = start[a] + (end[a] - start[a]) * i / steps
                time.sleep(duration / steps)
            with self._lock:
                if self._planner.qsize() == 0 and self.state in ("Run", "Jog"):
                    self.state = "Idle"
            self._planner.task_done()

    # -- reports -------------------------------------------------------------

    def _status(self):
        """Status report as GRBL 1.1 sends it with $10=1 (MPos, WCO only now and then)."""
        m = self.mpos
        fields = [
            self.state,
            f"MPos:{m['x']:.3f},{m['y']:.3f},{m['z']:.3f}",
            f"FS:{int(self.feed)},{int(self.spindle if self.spindle_mode != 'M5' else 0)}",
        ]
        if int(SETTINGS[10]) & 2:
            fields.insert(2, f"Bf:{PLANNER_BLOCKS - self._planner.qsize()},128")
        if self.probe and self.probe[3] and self.state == "Idle" and self.mpos["z"] <= self.probe[2]:
            fields.append("Pn:P")
        if self._reports % 10 == 0:
            o = self.offsets[self.wcs]
            fields.append(f"WCO:{o['x']:.3f},{o['y']:.3f},{o['z']:.3f}")
        elif self._reports % 10 == 1:
            fields.append("Ov:100,100,100")
        self._reports += 1
        return "<" + "|".join(fields) + ">"

    def _parser_state(self):
        return (
            f"[GC:{self.motion} {self.wcs} G17 {'G20' if self.inches else 'G21'} "
            f"{'G90' if self.absolute else 'G91'} G94 {self.spindle_mode} M9 T0 "
            f"F{self.feed:.0f} S{self.spindle:.0f}]"
        )


class GrblError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def serve(link_path, verbose=False, **kwargs):
    master, slave = os.openpty()
    tty.setraw(slave)
    slave_name = os.ttyname(slave)
    if os.path.lexists(link_path):
        os.unlink(link_path)
    os.symlink(slave_name, link_path)
    print(f"GRBL simulator on {link_path} -> {slave_name}", flush=True)

    write_lock = threading.Lock()

    def write(data):
        if verbose and not data.startswith(b"<"):
            print(f"  -> {data.decode().strip()}", flush=True)
        with write_lock:
            os.write(master, data)

    sim = GrblSimulator(write, **kwargs)
    lines = queue.Queue()

    def line_worker():
        while True:
            line = lines.get()
            if verbose:
                print(f"<-  {line}", flush=True)
            sim.line(line)

    threading.Thread(target=line_worker, daemon=True).start()

    buf = bytearray()
    try:
        while True:
            ready, _, _ = select.select([master], [], [], 0.5)
            if not ready:
                continue
            for byte in os.read(master, 1024):
                if sim.realtime(byte):
                    continue
                if byte in (0x0A, 0x0D):
                    if buf:
                        lines.put(buf.decode("ascii", errors="replace"))
                        buf.clear()
                else:
                    buf.append(byte)
    except KeyboardInterrupt:
        pass
    finally:
        os.unlink(link_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("link", help="path for the serial device symlink, e.g. /tmp/ttyGRBL")
    parser.add_argument("--homing", action="store_true", help="start in Alarm state like GRBL with $22=1")
    parser.add_argument("--time-scale", type=float, default=0.05,
                        help="fraction of real motion time to simulate (default 0.05)")
    parser.add_argument("--verbose", action="store_true", help="log received lines and replies (not status reports)")
    parser.add_argument("--surface-z", type=float, default=-1.0,
                        help="copper surface height in machine coordinates (default -1.0)")
    args = parser.parse_args()
    serve(args.link, verbose=args.verbose, homing=args.homing, time_scale=args.time_scale, surface_z=args.surface_z)


if __name__ == "__main__":
    sys.exit(main())
