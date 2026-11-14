# -*- coding: utf-8 -*-
"""
Gerber & Excellon Parser

Parses Gerber (RS-274X) files using pygerber and Excellon drill files
with a custom parser. Extracts geometry for toolpath generation.
"""

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from shapely.geometry import (
    LineString,
    MultiPolygon,
    Point,
    Polygon,
)
from shapely.ops import unary_union

from .pcb_project import DrillInfo, LayerType, PCBLayer

_logger = logging.getLogger(__name__)


def _to_float(val) -> float:
    """Convert a pygerber Offset or other numeric value to float.

    pygerber >= 2.x uses Offset objects for coordinates and dimensions.
    These don't support float() directly but expose a .value property.
    """
    if hasattr(val, "value"):
        return float(val.value)
    return float(val)


# =============================================================================
# Gerber Parsing (via pygerber)
# =============================================================================


def parse_gerber_file(filepath: str) -> tuple[PCBLayer, list]:
    """
    Parse a Gerber file and extract geometry.

    Returns:
        tuple of (PCBLayer with metadata, list of Shapely geometries)
    """
    from pygerber.gerberx3.api.v2 import GerberFile

    _logger.info(f"Parsing Gerber file: {filepath}")

    gerber = GerberFile.from_file(filepath)
    parsed = gerber.parse()
    info = parsed.get_info()

    layer = PCBLayer(
        layer_type=LayerType.TOP_COPPER,  # caller should override
        filename=Path(filepath).name,
        filepath=filepath,
        min_x=float(info.min_x_mm),
        min_y=float(info.min_y_mm),
        max_x=float(info.max_x_mm),
        max_y=float(info.max_y_mm),
        width=float(info.width_mm),
        height=float(info.height_mm),
        parsed=True,
    )

    # Generate SVG preview
    try:
        from io import BytesIO
        buf = BytesIO()
        parsed.render_svg(buf)
        svg_bytes = buf.getvalue()
        layer.svg_preview = svg_bytes.decode("utf-8") if isinstance(svg_bytes, bytes) else str(svg_bytes)
    except Exception as e:
        _logger.warning(f"Could not render SVG preview: {e}")

    # Extract Shapely geometries from commands
    geometries = _extract_geometries(parsed)

    # Recompute layer bounds from our extracted geometries (more accurate
    # than pygerber's get_info() which may miscalculate arc bounding boxes)
    if geometries:
        valid_geoms = [g for g in geometries if g is not None and not g.is_empty]
        if valid_geoms:
            all_geom = unary_union(valid_geoms)
            bounds = all_geom.bounds  # (minx, miny, maxx, maxy)
            layer.min_x = bounds[0]
            layer.min_y = bounds[1]
            layer.max_x = bounds[2]
            layer.max_y = bounds[3]
            layer.width = bounds[2] - bounds[0]
            layer.height = bounds[3] - bounds[1]

    _logger.info(
        f"Parsed {Path(filepath).name}: "
        f"{layer.width:.2f} x {layer.height:.2f} mm, "
        f"{len(geometries)} geometries"
    )

    return layer, geometries


def _extract_geometries(parsed) -> list:
    """
    Extract Shapely geometries from parsed Gerber commands.

    Converts pygerber command objects (Flash2, Line2, Arc2, Region2)
    into Shapely geometries suitable for toolpath offset operations.
    """

    geometries = []
    buf = parsed._command_buffer
    skipped = 0

    for cmd in buf:
        try:
            geom = _command_to_shapely(cmd)
        except Exception as e:
            skipped += 1
            _logger.debug(f"Could not convert command {type(cmd).__name__}: {e}")
            continue
        if geom is None or geom.is_empty:
            continue
        if _is_clear(cmd):
            # Clear polarity (%LPC*%) erases what was drawn before it, e.g.
            # clearances cut out of a filled zone. Ignoring it would leave
            # copper where the board has none: shorts after milling.
            geometries = _erase(geometries, geom)
        else:
            geometries.append(geom)

    if skipped:
        _logger.warning(
            f"{skipped} Gerber object(s) could not be converted and are missing from the geometry"
        )
    return geometries


def _is_clear(cmd) -> bool:
    """True for objects drawn with clear polarity."""
    polarity = getattr(getattr(cmd, "transform", None), "polarity", None)
    return getattr(polarity, "name", "") in ("Clear", "ClearRegion")


def _erase(geometries: list, eraser) -> list:
    """Remove the area of ``eraser`` from every geometry."""
    if not eraser.is_valid:
        eraser = eraser.buffer(0)
    result = []
    for geom in geometries:
        if geom.intersects(eraser):
            if not geom.is_valid:
                geom = geom.buffer(0)
            geom = geom.difference(eraser)
            if geom.is_empty:
                continue
        result.append(geom)
    return result


def _command_to_shapely(cmd):
    """Convert a single pygerber command to a Shapely geometry."""
    cls_name = type(cmd).__name__

    if cls_name == "Flash2":
        pt = cmd.flash_point
        x, y = _to_float(pt.x), _to_float(pt.y)

        ap = cmd.aperture
        ap_type = type(ap).__name__

        # Macro aperture (e.g. RoundRect) — build shape from
        # the macro's sub-primitives instead of using a simple circle.
        if ap_type == "Macro2" and hasattr(ap, "command_buffer"):
            shape = _macro_aperture_to_shapely(ap)
            if shape is not None and not shape.is_empty:
                from shapely.affinity import translate
                return translate(shape, xoff=x, yoff=y)

        # Rectangle aperture — create a proper rectangle
        if ap_type == "Rectangle2" and hasattr(ap, "x_size") and hasattr(ap, "y_size"):
            return _rectangle_aperture_flash(ap, x, y)

        # Obround (oval/capsule) aperture
        if ap_type == "Obround2" and hasattr(ap, "x_size") and hasattr(ap, "y_size"):
            return _obround_aperture_flash(ap, x, y)

        # Circle aperture (or unknown) — model as circle based on diameter.
        radius = _aperture_radius(ap)
        if radius > 0:
            return Point(x, y).buffer(radius, quad_segs=32)
        return Point(x, y).buffer(0.05, quad_segs=16)  # minimum pad

    elif cls_name == "Line2":
        # Line trace between two points with aperture width
        sp, ep = cmd.start_point, cmd.end_point
        x1, y1 = _to_float(sp.x), _to_float(sp.y)
        x2, y2 = _to_float(ep.x), _to_float(ep.y)
        width = _aperture_radius(cmd.aperture) * 2
        if width <= 0:
            width = 0.1  # minimum trace width
        line = LineString([(x1, y1), (x2, y2)])
        return line.buffer(width / 2, cap_style="round", join_style="round")

    elif cls_name in ("Arc2", "CCArc2"):
        # Arc trace - approximate with line segments
        # Arc2 = clockwise (G02), CCArc2 = counterclockwise (G03)
        clockwise = (cls_name == "Arc2")
        sp, ep, cp = cmd.start_point, cmd.end_point, cmd.center_point
        x1, y1 = _to_float(sp.x), _to_float(sp.y)
        x2, y2 = _to_float(ep.x), _to_float(ep.y)
        cx, cy = _to_float(cp.x), _to_float(cp.y)
        width = _aperture_radius(cmd.aperture) * 2
        if width <= 0:
            width = 0.1

        arc_points = _arc_to_points(x1, y1, x2, y2, cx, cy, clockwise=clockwise)
        if len(arc_points) >= 2:
            line = LineString(arc_points)
            return line.buffer(width / 2, cap_style="round", join_style="round")

    elif cls_name == "Region2":
        # Region = filled polygon
        try:
            points = _region_to_points(cmd)
            if len(points) >= 3:
                return Polygon(points)
        except Exception as e:
            _logger.debug(f"Region conversion error: {e}")

    return None


def _rectangle_aperture_flash(aperture, x: float, y: float):
    """Create a rectangular polygon for a Rectangle2 aperture flash.

    The rectangle is centred at (x, y) with dimensions x_size × y_size.
    An optional rotation attribute (degrees) is applied.
    """
    from shapely.geometry import box
    from shapely.affinity import rotate, translate

    w = _to_float(aperture.x_size)
    h = _to_float(aperture.y_size)
    rect = box(-w / 2, -h / 2, w / 2, h / 2)

    rotation = 0.0
    if hasattr(aperture, "rotation") and aperture.rotation is not None:
        rotation = float(aperture.rotation)
    if rotation:
        rect = rotate(rect, rotation, origin=(0, 0), use_radians=False)

    return translate(rect, xoff=x, yoff=y)


def _obround_aperture_flash(aperture, x: float, y: float):
    """Create an obround (capsule / stadium) shape for an Obround2 flash.

    An obround pad is a rectangle with semicircular ends on the shorter
    sides.  We model it as a LineString along the long axis buffered by
    half the short dimension.
    """
    from shapely.affinity import rotate, translate

    w = _to_float(aperture.x_size)
    h = _to_float(aperture.y_size)

    if abs(w - h) < 1e-6:
        # Square obround = circle
        return Point(x, y).buffer(w / 2, quad_segs=32)

    if w > h:
        # Horizontal capsule
        half_len = (w - h) / 2
        line = LineString([(-half_len, 0), (half_len, 0)])
        shape = line.buffer(h / 2, quad_segs=32, cap_style="round")
    else:
        # Vertical capsule
        half_len = (h - w) / 2
        line = LineString([(0, -half_len), (0, half_len)])
        shape = line.buffer(w / 2, quad_segs=32, cap_style="round")

    rotation = 0.0
    if hasattr(aperture, "rotation") and aperture.rotation is not None:
        rotation = float(aperture.rotation)
    if rotation:
        shape = rotate(shape, rotation, origin=(0, 0), use_radians=False)

    return translate(shape, xoff=x, yoff=y)


def _aperture_radius(aperture) -> float:
    """Extract radius from a pygerber aperture object."""
    try:
        # Try different aperture attributes
        if hasattr(aperture, "diameter"):
            return _to_float(aperture.diameter) / 2
        if hasattr(aperture, "outer_diameter"):
            return _to_float(aperture.outer_diameter) / 2
        if hasattr(aperture, "width"):
            return _to_float(aperture.width) / 2
        # For complex apertures, try to get some size
        if hasattr(aperture, "x_size"):
            return _to_float(aperture.x_size) / 2
    except (TypeError, ValueError):
        pass
    return 0.05  # fallback minimum


def _macro_aperture_to_shapely(aperture):
    """Build a Shapely geometry from a Macro2 aperture's sub-primitives.

    Macro apertures (e.g. KiCad's ``RoundRect``) are composed of
    Region2 (polygon body), Flash2 (corner circles) and Line2
    (edge strips) sub-commands.  We convert each primitive and union
    them to produce the final pad shape at the origin.
    """
    parts = []
    for sub_cmd in aperture.command_buffer:
        cls = type(sub_cmd).__name__

        if cls == "Region2":
            try:
                pts = _region_to_points(sub_cmd)
                if len(pts) >= 3:
                    parts.append(Polygon(pts))
            except Exception as e:
                _logger.debug(f"Macro aperture region conversion error: {e}")

        elif cls == "Flash2":
            pt = sub_cmd.flash_point
            sx, sy = _to_float(pt.x), _to_float(pt.y)
            r = _aperture_radius(sub_cmd.aperture)
            if r > 0:
                parts.append(Point(sx, sy).buffer(r, quad_segs=32))

        elif cls == "Line2":
            sp, ep = sub_cmd.start_point, sub_cmd.end_point
            x1, y1 = _to_float(sp.x), _to_float(sp.y)
            x2, y2 = _to_float(ep.x), _to_float(ep.y)
            w = _aperture_radius(sub_cmd.aperture) * 2
            if w > 0 and (abs(x1 - x2) > 1e-7 or abs(y1 - y2) > 1e-7):
                line = LineString([(x1, y1), (x2, y2)])
                parts.append(line.buffer(w / 2, cap_style="flat"))

        elif cls in ("Arc2", "CCArc2"):
            clockwise = (cls == "Arc2")
            sp, ep, cp = sub_cmd.start_point, sub_cmd.end_point, sub_cmd.center_point
            x1, y1 = _to_float(sp.x), _to_float(sp.y)
            x2, y2 = _to_float(ep.x), _to_float(ep.y)
            ccx, ccy = _to_float(cp.x), _to_float(cp.y)
            w = _aperture_radius(sub_cmd.aperture) * 2
            if w > 0:
                arc_pts = _arc_to_points(x1, y1, x2, y2, ccx, ccy, clockwise=clockwise)
                if len(arc_pts) >= 2:
                    parts.append(LineString(arc_pts).buffer(w / 2, cap_style="flat"))

    if not parts:
        return None
    return unary_union(parts)


def _arc_to_points(x1, y1, x2, y2, cx, cy, clockwise=True, segments=64) -> list:
    """Convert arc to line segment points.

    Args:
        x1, y1: Arc start point
        x2, y2: Arc end point
        cx, cy: Arc center point
        clockwise: True for CW (G02), False for CCW (G03)
        segments: Number of line segments to approximate the arc
    """
    radius = math.sqrt((x1 - cx) ** 2 + (y1 - cy) ** 2)
    if radius < 1e-6:
        return [(x1, y1), (x2, y2)]

    start_angle = math.atan2(y1 - cy, x1 - cx)
    end_angle = math.atan2(y2 - cy, x2 - cx)

    # Handle full circle (start == end)
    if abs(x1 - x2) < 1e-6 and abs(y1 - y2) < 1e-6:
        sweep = -2 * math.pi if clockwise else 2 * math.pi
    elif clockwise:
        # CW: sweep must be negative
        sweep = end_angle - start_angle
        if sweep >= 0:
            sweep -= 2 * math.pi
    else:
        # CCW: sweep must be positive
        sweep = end_angle - start_angle
        if sweep <= 0:
            sweep += 2 * math.pi

    # Scale segments to arc length for consistent point density
    arc_len = abs(sweep) * radius
    actual_segments = max(segments, int(arc_len / 0.5))  # ~0.5mm per segment

    points = []
    for i in range(actual_segments + 1):
        t = i / actual_segments
        angle = start_angle + sweep * t
        px = cx + radius * math.cos(angle)
        py = cy + radius * math.sin(angle)
        points.append((px, py))

    return points


def _region_to_points(region_cmd) -> list:
    """Extract boundary points from a Region2 command."""
    points = []
    if hasattr(region_cmd, "command_buffer"):
        for sub_cmd in region_cmd.command_buffer:
            cls = type(sub_cmd).__name__
            if cls == "Line2":
                sp = sub_cmd.start_point
                points.append((_to_float(sp.x), _to_float(sp.y)))
            elif cls in ("Arc2", "CCArc2"):
                clockwise = (cls == "Arc2")
                sp = sub_cmd.start_point
                ep = sub_cmd.end_point
                cp = sub_cmd.center_point
                arc_pts = _arc_to_points(
                    _to_float(sp.x), _to_float(sp.y),
                    _to_float(ep.x), _to_float(ep.y),
                    _to_float(cp.x), _to_float(cp.y),
                    clockwise=clockwise,
                )
                points.extend(arc_pts[:-1])  # avoid duplicate

        # Close the polygon
        if points and points[0] != points[-1]:
            points.append(points[0])
    return points


# =============================================================================
# Excellon Drill File Parser
# =============================================================================

# Excellon format patterns
_HEADER_START = re.compile(r"^M48")
_HEADER_END = re.compile(r"^%$|^M95")
# Tool definition, optionally with feed/speed before the diameter: T1F00S00C0.8
_TOOL_DEF = re.compile(r"^T(\d+)(?:[FSBH][\d.]+)*C([\d.]+)")
_TOOL_SELECT = re.compile(r"^T(\d+)$")
# X and/or Y; a missing axis keeps its previous value (modal)
_COORD_LINE = re.compile(
    r"^(?:X([+-]?[\d.]+))?(?:Y([+-]?[\d.]+))?",
    re.IGNORECASE,
)
# METRIC|INCH [,LZ|,TZ] [,000.000]  (also M71 metric, M72 inch)
_UNIT_LINE = re.compile(
    r"^(METRIC|INCH|M71|M72)(?:,(LZ|TZ))?(?:,(0+)\.(0+))?",
    re.IGNORECASE,
)
# KiCad comments: ;FILE_FORMAT=3:3  and  ; FORMAT={3:3/ absolute / metric / ...}
_FILE_FORMAT = re.compile(r"^;\s*(?:FILE_FORMAT=|FORMAT=\{)(\d+):(\d+)")

# Slot G85, router up/down M15/M16/M17, routing moves G00-G03
_ROUTING = re.compile(r"G85|^M1[567]\b|^G0?[0-3](?:X|Y|$)", re.IGNORECASE)

# Integer and decimal digits when the file does not say (KiCad, gerbv)
_DEFAULT_DIGITS = {"metric": (3, 3), "inch": (2, 4)}


def _excellon_number(text: str, digits: tuple, zeros: Optional[str]) -> float:
    """
    Convert an Excellon coordinate to a float in file units.

    Numbers with a decimal point are taken as they are. Without one, the
    format decides: TZ (trailing zeros kept) is right-aligned, LZ (leading
    zeros kept, trailing ones dropped) is padded on the right.
    """
    if "." in text:
        return float(text)
    sign = -1.0 if text.startswith("-") else 1.0
    number = text.lstrip("+-")
    int_digits, dec_digits = digits
    if zeros == "LZ":
        number = number.ljust(int_digits + dec_digits, "0")
    return sign * int(number) / 10 ** dec_digits


def _slot_holes(start: tuple, end: tuple, diameter: float) -> list[tuple]:
    """
    Hole centres that drill a slot from ``start`` to ``end``.

    The holes are at most half a diameter apart, which leaves a scallop of
    about 7 % of the diameter on the slot walls. Order: first every second
    hole, then the ones in between. That way the first round drills into
    full material on both sides and the drill is not pushed sideways into a
    neighbouring hole.
    """
    length = math.hypot(end[0] - start[0], end[1] - start[1])
    if length < 1e-6 or diameter <= 0:
        return [start]
    steps = max(1, math.ceil(length / (diameter / 2.0)))
    if steps % 2:
        steps += 1  # an even count puts both slot ends into the first round
    points = [
        (start[0] + (end[0] - start[0]) * i / steps, start[1] + (end[1] - start[1]) * i / steps)
        for i in range(steps + 1)
    ]
    return points[0::2] + points[1::2]


@dataclass
class ExcellonTool:
    """A drill tool definition."""
    number: int
    diameter: float  # mm
    holes: list = field(default_factory=list)  # [(x, y), ...]
    slot_runs: list = field(default_factory=list)  # [(first index, count), ...]


def parse_excellon_file(filepath: str) -> tuple[PCBLayer, list[DrillInfo]]:
    """
    Parse an Excellon drill file.

    Returns:
        tuple of (PCBLayer with bounds, list of DrillInfo objects)
    """
    _logger.info(f"Parsing Excellon drill file: {filepath}")

    with open(filepath) as f:
        content = f.read()

    lines = content.splitlines()
    tools: dict[int, ExcellonTool] = {}
    current_tool: Optional[int] = None
    units = "metric"  # assume metric by default
    digits: Optional[tuple] = None  # from the file, else the unit default
    zeros: Optional[str] = None  # "LZ", "TZ" or unknown
    scale = 1.0
    x = y = 0.0
    unsupported_slots = 0
    drilled_slots = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = _FILE_FORMAT.match(line)
        if m:
            digits = (int(m.group(1)), int(m.group(2)))
            continue
        if line.startswith(";") or _HEADER_START.match(line) or _HEADER_END.match(line):
            continue

        # Unit and number format (can be in or outside header)
        m = _UNIT_LINE.match(line)
        if m:
            units = "inch" if m.group(1).upper() in ("INCH", "M72") else "metric"
            scale = 25.4 if units == "inch" else 1.0
            if m.group(2):
                zeros = m.group(2).upper()
            if m.group(3):
                digits = (len(m.group(3)), len(m.group(4)))
            continue

        # Tool definitions (in header)
        m = _TOOL_DEF.match(line)
        if m:
            num = int(m.group(1))
            dia = float(m.group(2)) * scale
            tools[num] = ExcellonTool(number=num, diameter=dia)
            continue

        # Tool selection (in body)
        m = _TOOL_SELECT.match(line)
        if m:
            current_tool = int(m.group(1))
            continue

        # Routed slots (G85, or M15/M16 with G00-G03 moves) are not drilled
        # holes; say so instead of drilling a single hole at their start
        if _ROUTING.search(line):
            upper = line.upper()
            if "G85" not in upper:
                # M15 lowers the router: one routed path
                unsupported_slots += upper.startswith("M15")
                continue
            # G85 is Excellon's drilled slot: a row of overlapping holes
            # from the first to the second coordinate
            start_text, end_text = upper.split("G85", 1)
            number_format = digits or _DEFAULT_DIGITS[units]
            ms, me = _COORD_LINE.match(start_text), _COORD_LINE.match(end_text)
            if ms.group(1):
                x = _excellon_number(ms.group(1), number_format, zeros) * scale
            if ms.group(2):
                y = _excellon_number(ms.group(2), number_format, zeros) * scale
            end_x, end_y = x, y
            if me.group(1):
                end_x = _excellon_number(me.group(1), number_format, zeros) * scale
            if me.group(2):
                end_y = _excellon_number(me.group(2), number_format, zeros) * scale
            if current_tool in tools:
                tool = tools[current_tool]
                slot = _slot_holes((x, y), (end_x, end_y), tool.diameter)
                tool.slot_runs.append((len(tool.holes), len(slot)))
                tool.holes.extend(slot)
                drilled_slots += 1
            x, y = end_x, end_y
            continue

        # Coordinate lines
        m = _COORD_LINE.match(line)
        if m and (m.group(1) or m.group(2)) and current_tool is not None:
            number_format = digits or _DEFAULT_DIGITS[units]
            if m.group(1):
                x = _excellon_number(m.group(1), number_format, zeros) * scale
            if m.group(2):
                y = _excellon_number(m.group(2), number_format, zeros) * scale

            if current_tool in tools:
                tools[current_tool].holes.append((x, y))

    # Build DrillInfo list
    drills = []
    all_x = []
    all_y = []

    for tool in sorted(tools.values(), key=lambda t: t.number):
        if tool.holes:
            drills.append(
                DrillInfo(
                    tool_number=tool.number,
                    diameter=tool.diameter,
                    holes=tool.holes,
                    count=len(tool.holes),
                    slot_runs=tool.slot_runs,
                )
            )
            for x, y in tool.holes:
                all_x.append(x)
                all_y.append(y)

    # Create layer with bounds
    layer = PCBLayer(
        layer_type=LayerType.DRILL,
        filename=Path(filepath).name,
        filepath=filepath,
        parsed=True,
    )

    if all_x and all_y:
        layer.min_x = min(all_x)
        layer.min_y = min(all_y)
        layer.max_x = max(all_x)
        layer.max_y = max(all_y)
        layer.width = layer.max_x - layer.min_x
        layer.height = layer.max_y - layer.min_y

    layer.unsupported_slots = unsupported_slots
    layer.drilled_slots = drilled_slots
    if unsupported_slots:
        _logger.warning(
            f"{Path(filepath).name}: {unsupported_slots} routed slot(s) are not supported; "
            "slots are drilled as a single hole at their start or left out"
        )

    total_holes = sum(d.count for d in drills)
    total_tools = len(drills)
    _logger.info(
        f"Parsed {Path(filepath).name}: "
        f"{total_tools} tools, {total_holes} holes"
    )

    return layer, drills


# =============================================================================
# Unified geometry merge
# =============================================================================


def merge_geometries(geometries: list) -> MultiPolygon:
    """
    Merge all geometries into a single MultiPolygon.
    This creates the copper fill that the isolation toolpath needs to clear around.
    """
    if not geometries:
        return MultiPolygon()

    valid = []
    for g in geometries:
        if g is None or g.is_empty:
            continue
        if not g.is_valid:
            g = g.buffer(0)  # fix invalid geometry
        valid.append(g)

    if not valid:
        return MultiPolygon()

    merged = unary_union(valid)

    if isinstance(merged, Polygon):
        return MultiPolygon([merged])
    elif isinstance(merged, MultiPolygon):
        return merged
    else:
        # Try to convert other geometry types
        polygons = []
        if hasattr(merged, "geoms"):
            for g in merged.geoms:
                if isinstance(g, Polygon):
                    polygons.append(g)
        return MultiPolygon(polygons) if polygons else MultiPolygon()
