"""
Gerber & Excellon Parser

Parses Gerber (RS-274X) files using pygerber and Excellon drill files
with a custom parser. Extracts geometry for toolpath generation.
"""

import itertools
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

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

    _install_pygerber_extensions()
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
    except Exception as e:  # noqa: BLE001 - preview is optional, parser errors are unpredictable; logged
        _logger.warning(f"Could not render SVG preview: {e}")

    # Extract Shapely geometries from commands
    stats: dict = {}
    geometries = _extract_geometries(parsed, stats)
    layer.unsupported_shapes = stats.get("unsupported", 0)
    layer.stroke_width = _max_stroke_width(parsed)
    ring_width = _board_ring_stroke_width(geometries)
    if ring_width > 0:
        # The ring's width is measured from its area (rounded corners make it
        # slightly off): snap it to the aperture width it was drawn with
        widths = _stroke_widths(parsed)
        nearest = min(widths, key=lambda w: abs(w - ring_width), default=None)
        if nearest is not None and abs(nearest - ring_width) <= 0.02 * max(nearest, 1e-9):
            ring_width = nearest
        layer.stroke_width = ring_width

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


def _extract_geometries(parsed, stats: dict | None = None) -> list:
    """
    Extract Shapely geometries from parsed Gerber commands.

    Converts pygerber command objects (Flash2, Line2, Arc2, Region2)
    into Shapely geometries suitable for toolpath offset operations.
    """

    geometries = []
    skipped = 0
    unsupported = 0

    for cmd in _iter_objects(parsed._command_buffer):
        try:
            geom = _command_to_shapely(cmd)
        except Exception as e:  # noqa: BLE001 - third-party geometry conversion on unknown files; logged
            skipped += 1
            _logger.debug(f"Could not convert command {type(cmd).__name__}: {e}")
            continue
        if geom is None or geom.is_empty:
            if type(cmd).__name__ == "Flash2":
                # A pad whose shape is unknown: it is missing from the copper
                unsupported += 1
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
    if unsupported:
        _logger.warning(
            f"{unsupported} pad(s) use an aperture shape that is not supported "
            f"(macro primitives such as moire or vector line) and are missing from the geometry"
        )
    if stats is not None:
        stats["unsupported"] = unsupported + skipped
    return geometries


def _stroke_widths(parsed) -> list[float]:
    """Stroke widths (mm) of all line and arc objects of the layer."""
    widths = []
    for cmd in _iter_objects(parsed._command_buffer):
        if type(cmd).__name__ in ("Line2", "Arc2", "CCArc2"):
            try:
                widths.append(_aperture_radius(cmd.aperture) * 2)
            except Exception as exc:  # noqa: BLE001 - unknown aperture types; skip this object
                _logger.debug("Skipping object without usable aperture width: %s", exc)
                continue
    return widths


def _max_stroke_width(parsed) -> float:
    """
    Widest line or arc stroke of the layer in mm (0 if it has none).

    Edge.Cuts is drawn with strokes whose centre line is the board edge, so
    the layer bounds are one half stroke wider than the board on each side.
    """
    return max(_stroke_widths(parsed), default=0.0)


def _board_ring_stroke_width(geometries) -> float:
    """
    Stroke width of the outline ring that defines the board (0 if none).

    Same reading as board_shape_from_outline: the largest closed stroke ring
    is the board edge, its width is area / mean of outer and inner length.
    A wider stroke elsewhere (e.g. a cutout) must not shift the board edge.
    """
    valid = [g for g in geometries if g is not None and not g.is_empty]
    if not valid:
        return 0.0
    merged = unary_union(valid)
    polys = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
    rings = [p for p in polys if isinstance(p, Polygon) and p.interiors]
    if not rings:
        return 0.0
    ring = max(rings, key=lambda p: Polygon(p.exterior).area)
    perimeter = ring.exterior.length + sum(r.length for r in ring.interiors)
    return 2 * ring.area / perimeter if perimeter else 0.0


def _iter_objects(commands):
    """
    Graphical objects in drawing order, with step-repeat (%SR%) and block
    aperture (%AB%) buffers unpacked. Every object keeps the polarity it was
    drawn with. Regions are objects themselves and not unpacked.
    """
    for cmd in commands:
        if type(cmd).__name__ == "BufferCommand2":
            yield from _iter_objects(cmd.command_buffer)
        else:
            yield cmd


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
        return _flash_shape(cmd.aperture, _to_float(pt.x), _to_float(pt.y))

    elif cls_name == "Line2":
        # Line trace between two points with aperture width
        sp, ep = cmd.start_point, cmd.end_point
        x1, y1 = _to_float(sp.x), _to_float(sp.y)
        x2, y2 = _to_float(ep.x), _to_float(ep.y)
        if type(cmd.aperture).__name__ == "Rectangle2":
            # A rectangular aperture is not rotated along the line: it keeps
            # its orientation and sweeps, so the stroke is the hull of the
            # rectangle at both ends
            ends = [_rectangle_aperture_flash(cmd.aperture, x1, y1),
                    _rectangle_aperture_flash(cmd.aperture, x2, y2)]
            return unary_union(ends).convex_hull
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
            return _region_polygon(cmd)
        except Exception as e:  # noqa: BLE001 - third-party region conversion on unknown files; logged
            _logger.debug(f"Region conversion error: {e}")

    return None


def _flash_shape(ap, x: float, y: float):
    """Shape of aperture ``ap`` flashed at (x, y), including its hole."""
    ap_type = type(ap).__name__
    shape = None

    # Macro aperture (e.g. RoundRect): build the shape from the macro's
    # sub-primitives instead of using a simple circle.
    if ap_type == "Macro2" and hasattr(ap, "command_buffer"):
        macro = _macro_aperture_to_shapely(ap)
        if macro is None or macro.is_empty:
            # Unknown macro shape: never replace it by a guessed dot
            return None
        from shapely.affinity import translate
        shape = translate(macro, xoff=x, yoff=y)
    elif ap_type == "Macro2":
        return None
    elif ap_type == "Polygon2" and hasattr(ap, "number_vertices"):
        shape = _polygon_aperture_flash(ap, x, y)
    elif ap_type == "Rectangle2" and hasattr(ap, "x_size") and hasattr(ap, "y_size"):
        shape = _rectangle_aperture_flash(ap, x, y)
    elif ap_type == "Obround2" and hasattr(ap, "x_size") and hasattr(ap, "y_size"):
        shape = _obround_aperture_flash(ap, x, y)
    if shape is None:
        # Circle aperture (or unknown): a circle of the diameter
        radius = _aperture_radius(ap)
        if radius > 0:
            shape = Point(x, y).buffer(radius, quad_segs=32)
        else:
            return Point(x, y).buffer(0.05, quad_segs=16)  # minimum pad

    # Standard apertures may have a round hole (C,2.0X1.0)
    hole = getattr(ap, "hole_diameter", None)
    if hole is not None and ap_type != "Macro2" and shape is not None:
        hole_d = _to_float(hole)
        if hole_d > 0:
            shape = shape.difference(Point(x, y).buffer(hole_d / 2, quad_segs=32))
    return shape


def _polygon_aperture_flash(aperture, x: float, y: float):
    """Regular n-gon of a Polygon2 (``P``) aperture or macro primitive 5.

    The outer diameter is the circumscribed circle; the first vertex lies on
    the +X axis rotated by the aperture rotation (degrees, CCW).
    """
    n = max(3, int(aperture.number_vertices))
    radius = _to_float(aperture.outer_diameter) / 2
    rotation = math.radians(float(getattr(aperture, "rotation", 0) or 0))
    pts = [
        (x + radius * math.cos(rotation + 2 * math.pi * i / n),
         y + radius * math.sin(rotation + 2 * math.pi * i / n))
        for i in range(n)
    ]
    return Polygon(pts)


def _rectangle_aperture_flash(aperture, x: float, y: float):
    """Create a rectangular polygon for a Rectangle2 aperture flash.

    The rectangle is centred at (x, y) with dimensions x_size × y_size.
    An optional rotation attribute (degrees) is applied.
    """
    from shapely.affinity import rotate, translate
    from shapely.geometry import box

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


def thermal_shape(cx, cy, outer, inner, gap, rotation_deg):
    """Thermal relief (macro primitive 7) as a Shapely geometry.

    A ring between ``inner`` and ``outer`` diameter, cut by two gaps of width
    ``gap`` that cross at the centre, rotated by ``rotation_deg`` about the
    macro origin. The centre is rotated with it (Gerber spec 4.12.4.7).
    """
    from shapely.affinity import rotate
    from shapely.geometry import box

    ring = Point(0, 0).buffer(outer / 2, quad_segs=32)
    if inner > 0:
        ring = ring.difference(Point(0, 0).buffer(inner / 2, quad_segs=32))
    if gap > 0:
        reach = outer + gap
        ring = ring.difference(box(-reach, -gap / 2, reach, gap / 2))
        ring = ring.difference(box(-gap / 2, -reach, gap / 2, reach))
    ring = rotate(ring, rotation_deg, origin=(0, 0))
    rad = math.radians(rotation_deg)
    from shapely.affinity import translate
    return translate(
        ring,
        xoff=cx * math.cos(rad) - cy * math.sin(rad),
        yoff=cx * math.sin(rad) + cy * math.cos(rad),
    )


_EXTENSIONS_INSTALLED = False


def _install_pygerber_extensions():
    """Teach pygerber the thermal macro primitive (code 7).

    pygerber ignores it, so a thermal pad would come out as nothing. The
    replacement hook emits the ring as dark regions into the macro buffer.
    Without a matching pygerber internals the pad stays unsupported and is
    reported on import instead.
    """
    global _EXTENSIONS_INSTALLED
    if _EXTENSIONS_INSTALLED:
        return
    _EXTENSIONS_INSTALLED = True
    try:
        from decimal import Decimal

        from pygerber.gerberx3.math.offset import Offset
        from pygerber.gerberx3.math.vector_2d import Vector2D
        from pygerber.gerberx3.parser2 import parser2hooks as h
        from pygerber.gerberx3.parser2.apertures2.circle2 import Circle2
        from pygerber.gerberx3.parser2.command_buffer2 import CommandBuffer2
        from pygerber.gerberx3.parser2.commands2.line2 import Line2
        from pygerber.gerberx3.parser2.commands2.region2 import Region2
        from pygerber.gerberx3.parser2.state2 import ApertureTransform
        from pygerber.gerberx3.state_enums import Mirroring, Polarity
        from pygerber.gerberx3.tokenizer.aperture_id import ApertureID

        def on_code_7_thermal(self, context, primitive):
            ev = lambda e: float(e.on_parser2_eval_expression(context))
            geom = thermal_shape(
                ev(primitive.center_x), ev(primitive.center_y),
                ev(primitive.outer_diameter), ev(primitive.inner_diameter),
                ev(primitive.gap), ev(primitive.rotation),
            )
            units = context.get_draw_units()
            transform = ApertureTransform(
                polarity=Polarity.Dark, mirroring=Mirroring.NoMirroring,
                rotation=Decimal("0.0"), scaling=Decimal("1.0"),
            )
            polys = list(geom.geoms) if hasattr(geom, "geoms") else [geom]
            for poly in polys:
                if poly.is_empty:
                    continue
                lines = []
                for ring in [poly.exterior, *poly.interiors]:
                    pts = list(ring.coords)
                    for a, b in itertools.pairwise(pts):
                        lines.append(Line2(
                            transform=transform,
                            aperture=Circle2(
                                identifier=ApertureID(self.get_next_id()),
                                diameter=Offset.NULL, hole_diameter=None,
                            ),
                            start_point=Vector2D(
                                x=Offset.new(Decimal(repr(a[0])), units),
                                y=Offset.new(Decimal(repr(a[1])), units),
                            ),
                            end_point=Vector2D(
                                x=Offset.new(Decimal(repr(b[0])), units),
                                y=Offset.new(Decimal(repr(b[1])), units),
                            ),
                        ))
                context.get_macro_eval_buffer().add_command(
                    Region2(transform=transform,
                            command_buffer=CommandBuffer2(lines).get_readonly())
                )

        h.Parser2Hooks.MacroEvalHooks.on_code_7_thermal = on_code_7_thermal
    except Exception as exc:  # noqa: BLE001 - pygerber internals; thermal pads are reported as unsupported
        _logger.warning("Thermal macro support unavailable: %s", exc)


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
    shape = None
    for sub_cmd in aperture.command_buffer:
        cls = type(sub_cmd).__name__
        part = None

        if cls == "Region2":
            try:
                part = _region_polygon(sub_cmd)
            except Exception as e:  # noqa: BLE001 - third-party region conversion on unknown files; logged
                _logger.debug(f"Macro aperture region conversion error: {e}")

        elif cls == "Flash2":
            pt = sub_cmd.flash_point
            part = _flash_shape(sub_cmd.aperture, _to_float(pt.x), _to_float(pt.y))

        elif cls == "Line2":
            sp, ep = sub_cmd.start_point, sub_cmd.end_point
            x1, y1 = _to_float(sp.x), _to_float(sp.y)
            x2, y2 = _to_float(ep.x), _to_float(ep.y)
            w = _aperture_radius(sub_cmd.aperture) * 2
            if w > 0 and (abs(x1 - x2) > 1e-7 or abs(y1 - y2) > 1e-7):
                line = LineString([(x1, y1), (x2, y2)])
                part = line.buffer(w / 2, cap_style="flat")

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
                    part = LineString(arc_pts).buffer(w / 2, cap_style="flat")

        if part is None or part.is_empty:
            continue
        if _is_clear(sub_cmd):
            # Exposure off: the primitive removes what the macro drew so far
            if shape is not None:
                shape = shape.difference(part)
        else:
            shape = part if shape is None else shape.union(part)

    if shape is None or shape.is_empty:
        return None
    return shape


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


def _region_contours(region_cmd) -> list[list[tuple]]:
    """
    Closed contours of a Region2 command.

    One G36 region can hold several contours; each D02 move starts the next
    one. The pygerber children carry no move command, so a contour ends
    wherever the next segment does not start at the end of the previous one.
    """
    contours: list[list[tuple]] = []
    points: list[tuple] = []
    last_end = None
    for sub_cmd in getattr(region_cmd, "command_buffer", []):
        cls = type(sub_cmd).__name__
        if cls not in ("Line2", "Arc2", "CCArc2"):
            continue
        sp, ep = sub_cmd.start_point, sub_cmd.end_point
        start = (_to_float(sp.x), _to_float(sp.y))
        end = (_to_float(ep.x), _to_float(ep.y))
        if last_end is not None and (
            abs(start[0] - last_end[0]) > 1e-9 or abs(start[1] - last_end[1]) > 1e-9
        ):
            contours.append(points)
            points = []
        if cls == "Line2":
            points.append(start)
        else:
            cp = sub_cmd.center_point
            arc_pts = _arc_to_points(
                start[0], start[1], end[0], end[1],
                _to_float(cp.x), _to_float(cp.y),
                clockwise=(cls == "Arc2"),
            )
            points.extend(arc_pts[:-1])  # the end is the next start
        last_end = end
    if points:
        contours.append(points)

    closed = []
    for contour in contours:
        if len(contour) >= 3:
            if contour[0] != contour[-1]:
                contour = contour + [contour[0]]
            closed.append(contour)
    return closed


def _region_polygon(region_cmd):
    """
    Polygon of a Region2 command.

    Every contour is dark and overlapping contours merge (Gerber spec). A
    contour lying completely inside another one is read as a hole (lenient
    reading of nested contours; the spec forbids them), nested once more it
    is dark again.
    """
    polys = []
    for contour in _region_contours(region_cmd):
        poly = Polygon(contour)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not poly.is_empty:
            polys.append(poly)
    if not polys:
        return None
    dark, holes = [], []
    for i, poly in enumerate(polys):
        depth = sum(
            1 for j, other in enumerate(polys)
            if j != i and other.area > poly.area * (1 + 1e-9) and other.contains(poly)
        )
        (holes if depth % 2 else dark).append(poly)
    result = unary_union(dark)
    if holes:
        result = result.difference(unary_union(holes))
    return result


# =============================================================================
# Excellon Drill File Parser
# =============================================================================

# Excellon format patterns
_HEADER_START = re.compile(r"^M48")
_HEADER_END = re.compile(r"^%$|^M95")
# Tool definition, optionally with feed/speed before the diameter: T1F00S00C0.8
_TOOL_DEF = re.compile(r"^T(\d+)(?:[FSBH][\d.]+)*C([\d.]+)")
_TOOL_SELECT = re.compile(r"^T(\d+)(?:[FSBH][\d.]+)*$")
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


def _excellon_number(text: str, digits: tuple, zeros: str | None) -> float:
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
    current_tool: int | None = None
    units = "metric"  # assume metric by default
    digits: tuple | None = None  # from the file, else the unit default
    zeros: str | None = None  # "LZ", "TZ" or unknown
    scale = 1.0
    x = y = 0.0
    incremental = False  # G91 / ICI: coordinates are offsets to the last position
    router_down = False  # between M15 and M16/M17: routed path, not holes
    unsupported_slots = 0
    drilled_slots = 0

    def move_to(text_x, text_y, number_format):
        """Update (x, y) from coordinate words, absolute or incremental."""
        nonlocal x, y
        if text_x:
            value = _excellon_number(text_x, number_format, zeros) * scale
            x = x + value if incremental else value
        if text_y:
            value = _excellon_number(text_y, number_format, zeros) * scale
            y = y + value if incremental else value

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

        # Incremental / absolute coordinate mode
        upper_line = line.upper()
        if re.match(r"^(?:G91|ICI,ON)\b", upper_line):
            incremental = True
            continue
        if re.match(r"^(?:G90|ICI,OFF)\b", upper_line):
            incremental = False
            continue

        # Tool definitions. In the body (T1C0.8) the definition also selects
        # the tool for the coordinates that follow.
        m = _TOOL_DEF.match(line)
        if m:
            num = int(m.group(1))
            dia = float(m.group(2)) * scale
            tools[num] = ExcellonTool(number=num, diameter=dia)
            current_tool = num
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
                # M15 lowers the router: one routed path until M16/M17
                if upper.startswith("M15"):
                    unsupported_slots += 1
                    router_down = True
                elif upper.startswith(("M16", "M17")):
                    router_down = False
                else:
                    # G00-G03 move: the position moves on (I/J/A are not read)
                    mm = _COORD_LINE.match(upper[3:])
                    if mm and (mm.group(1) or mm.group(2)):
                        move_to(mm.group(1), mm.group(2), digits or _DEFAULT_DIGITS[units])
                continue
            # G85 is Excellon's drilled slot: a row of overlapping holes
            # from the first to the second coordinate
            start_text, end_text = upper.split("G85", 1)
            number_format = digits or _DEFAULT_DIGITS[units]
            ms, me = _COORD_LINE.match(start_text), _COORD_LINE.match(end_text)
            move_to(ms.group(1), ms.group(2), number_format)
            start = (x, y)
            move_to(me.group(1), me.group(2), number_format)
            end_x, end_y = x, y
            x, y = start
            if current_tool in tools:
                tool = tools[current_tool]
                slot = _slot_holes((x, y), (end_x, end_y), tool.diameter)
                tool.slot_runs.append((len(tool.holes), len(slot)))
                tool.holes.extend(slot)
                drilled_slots += 1
            x, y = end_x, end_y
            continue

        # Coordinate lines (modal moves of a routed path are no holes)
        m = _COORD_LINE.match(line)
        if router_down:
            if m and (m.group(1) or m.group(2)):
                move_to(m.group(1), m.group(2), digits or _DEFAULT_DIGITS[units])
            continue
        if m and (m.group(1) or m.group(2)) and current_tool is not None:
            number_format = digits or _DEFAULT_DIGITS[units]
            move_to(m.group(1), m.group(2), number_format)

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
