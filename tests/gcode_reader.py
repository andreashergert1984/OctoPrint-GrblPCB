"""Minimal G-code reader for tests: replays moves with modal state."""
import itertools
import math
import re

_WORD = re.compile(r"([GXYZFMTS])(-?\d+\.?\d*)")


def moves(gcode):
    """List of dicts: start, end (x, y, z), feed, rapid, line."""
    x = y = 0.0
    z = 5.0
    feed = 0.0
    out = []
    for raw in gcode.splitlines():
        line = raw.split(";")[0].strip()
        if not line:
            continue
        words = {}
        for letter, value in _WORD.findall(line):
            words.setdefault(letter, float(value))
        if "F" in words:
            feed = words["F"]
        nx, ny, nz = words.get("X", x), words.get("Y", y), words.get("Z", z)
        if (nx, ny, nz) != (x, y, z):
            out.append({
                "start": (x, y, z), "end": (nx, ny, nz), "feed": feed,
                "rapid": words.get("G") == 0, "line": line,
            })
        x, y, z = nx, ny, nz
    return out


def cutting_paths(gcode):
    """Polylines of XY moves below Z 0, split when the tool goes up."""
    paths, current = [], None
    for m in moves(gcode):
        (x0, y0, z0), (x1, y1, z1) = m["start"], m["end"]
        if z0 < 0 and z1 < 0 and (x0, y0) != (x1, y1) and not m["rapid"]:
            if current is None:
                current = [(x0, y0)]
                paths.append(current)
            current.append((x1, y1))
        elif z1 >= 0:
            current = None
    return paths


def signed_area(path):
    """> 0: counter-clockwise."""
    return sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in itertools.pairwise(path)) / 2


def length(path):
    return sum(math.hypot(x1 - x0, y1 - y0) for (x0, y0), (x1, y1) in itertools.pairwise(path))
