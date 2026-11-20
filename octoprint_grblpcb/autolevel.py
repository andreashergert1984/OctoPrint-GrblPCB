"""
Autoleveling Module

Implements PCB surface probing and G-code Z-correction:
1. Generate a grid of probe points over the board area
2. Execute probing sequence via GRBL connector
3. Build height map from probe results
4. Apply bilinear interpolation to correct Z values in G-code
"""

import logging
import math
import re
from dataclasses import dataclass, field

import numpy as np
from scipy.interpolate import RegularGridInterpolator

_logger = logging.getLogger(__name__)


@dataclass
class ProbePoint:
    """A single probe point with X, Y target and measured Z."""
    x: float
    y: float
    z: float = 0.0
    probed: bool = False

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "z": self.z, "probed": self.probed}

    @classmethod
    def from_dict(cls, data: dict) -> "ProbePoint":
        return cls(**data)


@dataclass
class HeightMap:
    """
    Height map from probe measurements.

    Stores a regular grid of Z measurements over the board surface.
    Supports bilinear interpolation for arbitrary X, Y positions.
    """
    # Grid definition
    min_x: float = 0.0
    min_y: float = 0.0
    max_x: float = 0.0
    max_y: float = 0.0
    grid_x: int = 5  # number of columns
    grid_y: int = 5  # number of rows

    # Probe data
    points: list = field(default_factory=list)  # flat list of ProbePoint
    completed: bool = False

    # Work offset (WCO, machine coordinates of the work zero) while probing.
    # The heights only apply to the board as it was clamped then.
    work_offset: dict | None = None
    probed_at: float | None = None  # time.time() when probing started

    # Interpolator (not serialized)
    _interpolator: RegularGridInterpolator | None = field(
        default=None, repr=False
    )

    def generate_grid(
        self,
        min_x: float,
        min_y: float,
        max_x: float,
        max_y: float,
        grid_x: int = 5,
        grid_y: int = 5,
        margin: float = 1.0,
    ):
        """
        Generate probe grid points over the board area.

        Args:
            min_x, min_y: board origin
            max_x, max_y: board extent
            grid_x, grid_y: number of probe points in each direction
            margin: extra margin around the board (mm)
        """
        self.min_x = min_x - margin
        self.min_y = min_y - margin
        self.max_x = max_x + margin
        self.max_y = max_y + margin
        self.grid_x = grid_x
        self.grid_y = grid_y

        self.points = []
        x_step = (self.max_x - self.min_x) / max(1, grid_x - 1)
        y_step = (self.max_y - self.min_y) / max(1, grid_y - 1)

        for row in range(grid_y):
            y = self.min_y + row * y_step
            # Serpentine pattern for efficient probing
            x_range = range(grid_x) if row % 2 == 0 else range(grid_x - 1, -1, -1)
            for col in x_range:
                x = self.min_x + col * x_step
                self.points.append(ProbePoint(x=x, y=y))

        _logger.info(
            f"Generated {len(self.points)} probe points: "
            f"X[{self.min_x:.1f}..{self.max_x:.1f}] "
            f"Y[{self.min_y:.1f}..{self.max_y:.1f}] "
            f"grid {grid_x}x{grid_y}"
        )

    def set_probe_result(self, index: int, z: float):
        """Record a probe measurement at the given index."""
        if 0 <= index < len(self.points):
            self.points[index].z = z
            self.points[index].probed = True
            _logger.debug(
                f"Probe point {index}: "
                f"X{self.points[index].x:.3f} "
                f"Y{self.points[index].y:.3f} "
                f"Z{z:.4f}"
            )

    def is_complete(self) -> bool:
        """Check if all points have been probed."""
        return all(p.probed for p in self.points)

    def can_resume(self, current_offset: dict) -> bool:
        """
        True if an interrupted measurement can be continued: some points are
        still open and the XY work zero is the one it was started with.
        """
        if self.is_complete() or not any(p.probed for p in self.points):
            return False
        return height_map_origin_check(self, current_offset) is None

    def reset_measurements(self):
        """Forget all measured heights; the grid stays."""
        for p in self.points:
            p.z = 0.0
            p.probed = False
        self.completed = False
        self._interpolator = None

    def next_unprobed_index(self) -> int:
        """Get index of next unprobed point, or -1 if all done."""
        for i, p in enumerate(self.points):
            if not p.probed:
                return i
        return -1

    def build_interpolator(self):
        """
        Build the 2D interpolation function from probe data.

        Uses scipy's RegularGridInterpolator for fast bilinear interpolation.
        The probe data must form a complete regular grid.
        """
        if not self.is_complete():
            _logger.warning("Height map not complete, cannot build interpolator")
            return

        # Reconstruct grid in sorted order
        x_vals = sorted({p.x for p in self.points})
        y_vals = sorted({p.y for p in self.points})

        if len(x_vals) < 2 or len(y_vals) < 2:
            _logger.warning("Need at least 2x2 grid for interpolation")
            return

        # Build Z data array
        z_data = np.zeros((len(y_vals), len(x_vals)))
        # Index lookup
        point_map = {(round(p.x, 6), round(p.y, 6)): p.z for p in self.points}

        for j, y in enumerate(y_vals):
            for i, x in enumerate(x_vals):
                key = (round(x, 6), round(y, 6))
                z_data[j, i] = point_map.get(key, 0.0)

        self._interpolator = RegularGridInterpolator(
            (np.array(y_vals), np.array(x_vals)),
            z_data,
            method="linear",
            bounds_error=False,
            fill_value=None,  # extrapolate
        )

        self.completed = True
        _logger.info(
            f"Height map interpolator built: "
            f"Z range [{z_data.min():.4f}, {z_data.max():.4f}]"
        )

    def get_z(self, x: float, y: float) -> float:
        """
        Get interpolated Z correction at a given X, Y position.

        Returns the Z offset from the nominal zero surface.
        """
        if self._interpolator is None:
            return 0.0

        # Clamp to grid bounds
        x = max(self.min_x, min(self.max_x, x))
        y = max(self.min_y, min(self.max_y, y))

        result = self._interpolator(np.array([[y, x]]))[0]
        return float(result)

    def to_dict(self) -> dict:
        return {
            "min_x": self.min_x,
            "min_y": self.min_y,
            "max_x": self.max_x,
            "max_y": self.max_y,
            "grid_x": self.grid_x,
            "grid_y": self.grid_y,
            "points": [p.to_dict() for p in self.points],
            "completed": self.completed,
            "work_offset": self.work_offset,
            "probed_at": self.probed_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HeightMap":
        hm = cls(
            min_x=data["min_x"],
            min_y=data["min_y"],
            max_x=data["max_x"],
            max_y=data["max_y"],
            grid_x=data["grid_x"],
            grid_y=data["grid_y"],
            completed=data.get("completed", False),
            work_offset=data.get("work_offset"),
            probed_at=data.get("probed_at"),
        )
        hm.points = [ProbePoint.from_dict(p) for p in data.get("points", [])]
        if hm.completed:
            hm.build_interpolator()
        return hm


ORIGIN_TOLERANCE_MM = 0.05


def height_map_origin_check(height_map: HeightMap, current_offset: dict) -> dict | None:
    """
    Check that the height map belongs to the current work zero.

    Only X and Y are compared: after a tool change Z is zeroed again at the
    same spot, the relative heights stay valid.

    Returns None if it matches, otherwise a warning dict with a ``code``.
    """
    if not height_map.work_offset:
        return {"code": "heightmap_origin_unknown"}
    dx = current_offset.get("x", 0.0) - height_map.work_offset.get("x", 0.0)
    dy = current_offset.get("y", 0.0) - height_map.work_offset.get("y", 0.0)
    if abs(dx) > ORIGIN_TOLERANCE_MM or abs(dy) > ORIGIN_TOLERANCE_MM:
        return {"code": "heightmap_origin_moved", "dx": round(dx, 3), "dy": round(dy, 3)}
    return None


def height_map_side_check(
    height_map: HeightMap, mirrored: bool, alignment_probed_at: float | None
) -> dict | None:
    """
    Check that the height map was measured on the side being milled.

    Probing the alignment holes marks the flip: it happens after the board
    was turned over. A back-side (mirrored) operation needs a height map
    measured after that, a front-side operation one measured before.

    Returns None if it fits, otherwise a warning dict with a ``code``.
    """
    probed_at = height_map.probed_at
    if mirrored:
        if alignment_probed_at is None or probed_at is None:
            return {"code": "heightmap_side_unknown"}
        if probed_at < alignment_probed_at:
            return {"code": "heightmap_from_front"}
    elif alignment_probed_at is not None and probed_at is not None and probed_at > alignment_probed_at:
        return {"code": "heightmap_from_back"}
    return None


# =============================================================================
# Probe Sequence Generation
# =============================================================================


def generate_probe_gcode(
    height_map: HeightMap,
    probe_depth: float = -5.0,
    probe_feed: float = 25.0,
    safe_z: float = 5.0,
    travel_z: float = 2.0,
    z_max_feed: float = 120.0,
) -> str:
    """
    Generate G-code for the probing sequence.

    This is NOT sent directly — instead it's used to show the user
    what will happen. The actual probing is done point-by-point
    via the connector's probe command.
    """
    lines = []
    lines.append("; === Autolevel Probe Sequence ===")
    lines.append(f"; Grid: {height_map.grid_x} x {height_map.grid_y}")
    lines.append(f"; Points: {len(height_map.points)}")
    lines.append(f"; Probe depth: {probe_depth} mm")
    lines.append(f"; Probe feed: {probe_feed} mm/min")
    lines.append("")

    lines.append("G21 ; mm mode")
    lines.append("G90 ; absolute positioning")
    lines.append(f"G1 Z{safe_z:.3f} F{z_max_feed:.0f} ; safe Z")
    lines.append("")

    for i, point in enumerate(height_map.points):
        lines.append(f"; Probe point {i + 1}/{len(height_map.points)}")
        lines.append(f"G0 X{point.x:.4f} Y{point.y:.4f}")
        lines.append(f"G1 Z{travel_z:.3f} F{z_max_feed:.0f}")
        lines.append(f"G38.2 Z{probe_depth} F{probe_feed}")
        lines.append("; >>> Record Z here <<<")
        lines.append(f"G1 Z{travel_z:.3f} F{z_max_feed:.0f}")
        lines.append("")

    lines.append(f"G1 Z{safe_z:.3f} F{z_max_feed:.0f}")
    lines.append("G0 X0 Y0")

    return "\n".join(lines)


# =============================================================================
# G-code Z-Correction (Autoleveling)
# =============================================================================

# Regex for G-code line parsing
_IS_MOVE = re.compile(r"^\s*G0?[01](?:\s|[XYZF])", re.IGNORECASE)
_IS_ARC = re.compile(r"^\s*G0?([23])(?![\d.])", re.IGNORECASE)


def apply_autolevel(gcode: str, height_map: HeightMap, max_segment: float = 1.0) -> str:
    """
    Apply height map correction to G-code.

    For each G-code line with Z movement, adjusts Z by the interpolated
    surface height at that X, Y position. Long line segments are split
    into shorter segments so the Z correction follows the surface.

    Args:
        gcode: input G-code string
        height_map: completed HeightMap with interpolator
        max_segment: maximum segment length (mm) for interpolation accuracy

    Returns:
        corrected G-code string
    """
    if not height_map.completed or height_map._interpolator is None:
        _logger.warning("Height map not ready, rebuilding interpolator...")
        height_map.build_interpolator()
        if height_map._interpolator is None:
            _logger.error("Cannot apply autolevel: no interpolator")
            return gcode

    _logger.info("Applying autolevel Z-correction to G-code")

    output_lines = []
    current_x = 0.0
    current_y = 0.0
    current_z = 0.0

    for line in gcode.splitlines():
        stripped = line.strip()

        # Pass through comments and empty lines
        if not stripped or stripped.startswith(";"):
            output_lines.append(line)
            continue

        # Arcs: below the surface they become short G1 segments that follow
        # the height map; either way the tracked position is the arc end point
        if _IS_ARC.match(stripped):
            arc_lines, (current_x, current_y, current_z) = _autolevel_arc(
                stripped, current_x, current_y, current_z, height_map, max_segment
            )
            output_lines.extend(arc_lines if arc_lines else [line])
            continue

        # Only modify G0/G1 moves
        if not _IS_MOVE.match(stripped):
            output_lines.append(line)
            continue

        # Parse coordinates
        new_x, new_y, new_z = _parse_xyz(stripped, current_x, current_y, current_z)

        # Check if this is a cutting move (Z < 0)
        is_g1 = stripped.startswith(("G1", "G01"))
        is_g0 = stripped.startswith("G0") and not stripped.startswith("G01")

        if is_g0:
            # Rapid move — apply Z correction at destination only
            z_correction = height_map.get_z(new_x, new_y)
            corrected_z = new_z + z_correction if new_z <= 0 else new_z
            corrected_line = _rebuild_gcode_line(
                stripped, new_x, new_y, corrected_z
            )
            output_lines.append(corrected_line)
        elif is_g1:
            # Linear move — subdivide for accuracy if Z is in cutting range
            if new_z <= 0 or current_z <= 0:
                # Subdivide the segment
                segments = _subdivide_segment(
                    current_x, current_y, current_z,
                    new_x, new_y, new_z,
                    max_segment,
                )

                # Extract feed rate from original line
                feed = _extract_feed(stripped)

                for i, (sx, sy, sz) in enumerate(segments):
                    z_correction = height_map.get_z(sx, sy)
                    corrected_z = sz + z_correction
                    seg_line = f"G1 X{sx:.4f} Y{sy:.4f} Z{corrected_z:.4f}"
                    if feed and i == 0:
                        seg_line += f" F{feed}"
                    output_lines.append(seg_line)
            else:
                # Above surface, no correction needed
                output_lines.append(line)
        else:
            output_lines.append(line)

        current_x = new_x
        current_y = new_y
        current_z = new_z

    _logger.info(
        f"Autolevel applied: {len(output_lines)} lines "
        f"(was {len(gcode.splitlines())})"
    )
    return "\n".join(output_lines)


def _autolevel_arc(
    line: str, x0: float, y0: float, z0: float, height_map, max_segment: float
) -> tuple[list[str], tuple[float, float, float]]:
    """
    Level one G2/G3 arc (XY plane, centre given as I/J or radius as R).

    Returns ``(replacement lines, new position)``. The replacement is empty
    when the line is to be kept as it is (the arc stays above the surface, or
    it cannot be interpreted); the position is the arc end point in every case.
    """
    x1, y1, z1 = _parse_xyz(line, x0, y0, z0)
    clockwise = _IS_ARC.match(line).group(1) == "2"
    end = (x1, y1, z1)

    def word(letter):
        m = re.search(rf"{letter}([+-]?\d+\.?\d*)", line, re.IGNORECASE)
        return float(m.group(1)) if m else None

    if z0 > 0 and z1 > 0:
        return [], end  # above the surface: no correction needed
    i_off, j_off, radius = word("I"), word("J"), word("R")
    if i_off is not None or j_off is not None:
        cx, cy = x0 + (i_off or 0.0), y0 + (j_off or 0.0)
    elif radius is not None:
        chord = math.hypot(x1 - x0, y1 - y0)
        if chord < 1e-9 or abs(radius) < chord / 2 - 1e-9:
            return [], end
        h = math.sqrt(max(radius * radius - (chord / 2) ** 2, 0.0))
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        ux, uy = (x1 - x0) / chord, (y1 - y0) / chord
        # Centre on the left of the chord for a short CCW / long CW arc
        sign = 1.0 if (not clockwise) == (radius > 0) else -1.0
        cx, cy = mx - uy * h * sign, my + ux * h * sign
    else:
        return [], end

    r = math.hypot(x0 - cx, y0 - cy)
    if r < 1e-9:
        return [], end
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    sweep = a1 - a0
    if clockwise:
        if sweep >= -1e-12:
            sweep -= 2 * math.pi
    elif sweep <= 1e-12:
        sweep += 2 * math.pi

    steps = max(2, math.ceil(abs(sweep) * r / max_segment))
    feed = _extract_feed(line)
    out = []
    for n in range(1, steps + 1):
        t = n / steps
        if n == steps:
            px, py = x1, y1
        else:
            ang = a0 + sweep * t
            px, py = cx + r * math.cos(ang), cy + r * math.sin(ang)
        pz = z0 + (z1 - z0) * t
        seg = f"G1 X{px:.4f} Y{py:.4f} Z{pz + height_map.get_z(px, py):.4f}"
        if feed and n == 1:
            seg += f" F{feed}"
        out.append(seg)
    return out, end


def _parse_xyz(
    line: str, default_x: float, default_y: float, default_z: float
) -> tuple[float, float, float]:
    """Parse X, Y, Z values from a G-code line."""
    x, y, z = default_x, default_y, default_z

    m = re.search(r"X([+-]?\d+\.?\d*)", line)
    if m:
        x = float(m.group(1))
    m = re.search(r"Y([+-]?\d+\.?\d*)", line)
    if m:
        y = float(m.group(1))
    m = re.search(r"Z([+-]?\d+\.?\d*)", line)
    if m:
        z = float(m.group(1))

    return x, y, z


def _extract_feed(line: str) -> str | None:
    """Extract feed rate from G-code line."""
    m = re.search(r"F([+-]?\d+\.?\d*)", line)
    return m.group(1) if m else None


def _rebuild_gcode_line(
    original: str, x: float, y: float, z: float
) -> str:
    """Rebuild a G-code move line with corrected coordinates."""
    # Determine G0 or G1
    cmd = "G0" if original.startswith("G0") and not original.startswith("G01") else "G1"

    parts = [cmd]

    if re.search(r"X[+-]?\d", original):
        parts.append(f"X{x:.4f}")
    if re.search(r"Y[+-]?\d", original):
        parts.append(f"Y{y:.4f}")
    if re.search(r"Z[+-]?\d", original):
        parts.append(f"Z{z:.4f}")

    feed = _extract_feed(original)
    if feed:
        parts.append(f"F{feed}")

    return " ".join(parts)


def _subdivide_segment(
    x1: float, y1: float, z1: float,
    x2: float, y2: float, z2: float,
    max_length: float,
) -> list[tuple[float, float, float]]:
    """
    Subdivide a line segment into sub-segments no longer than max_length.
    Returns list of endpoint coordinates (not including start point).
    """
    dx = x2 - x1
    dy = y2 - y1
    dz = z2 - z1
    length = math.sqrt(dx * dx + dy * dy)

    if length <= max_length or length < 0.001:
        return [(x2, y2, z2)]

    num_segments = max(1, math.ceil(length / max_length))
    points = []

    for i in range(1, num_segments + 1):
        t = i / num_segments
        points.append((
            x1 + dx * t,
            y1 + dy * t,
            z1 + dz * t,
        ))

    return points
