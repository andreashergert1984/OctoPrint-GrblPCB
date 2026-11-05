# -*- coding: utf-8 -*-
"""
Toolpath Generation

Generates G-code toolpaths from parsed Gerber/Excellon geometries:
- Isolation milling: offset copper geometry outward to create isolation trenches
- Drilling: generate drill G-code with peck drilling support
- Outline cutting: cut board outline with optional tabs
- Double-sided alignment: compute and apply coordinate transforms
"""

import logging
import math
import re
from dataclasses import dataclass
from typing import Optional

from shapely.geometry import (
    LinearRing,
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
    box,
)
from shapely.ops import unary_union

_logger = logging.getLogger(__name__)


# =============================================================================
# Isolation Milling Toolpath
# =============================================================================


@dataclass
class ToolpathSettings:
    """Settings for toolpath generation."""

    # Tool
    tool_diameter: float = 0.1  # mm (V-bit tip)
    tool_angle: float = 60.0  # degrees (V-bit included angle)
    cut_depth: float = 0.1  # mm (positive value, will be negated in G-code)

    # Milling
    feed_rate: float = 500.0  # mm/min
    plunge_rate: float = 100.0  # mm/min
    spindle_speed: int = 10000  # RPM
    safe_z: float = 5.0  # mm
    travel_z: float = 2.0  # mm (Z for rapid moves between cuts)

    # Isolation
    passes: int = 1  # number of isolation passes
    pass_overlap: float = 0.0  # mm extra offset per pass (0 = no overlap)
    isolation_width: float = 0.0  # if > 0, overrides tool_diameter for offset
    depth_per_pass: float = 0.0  # mm per depth pass (0 = full depth in one pass)
    max_width: float = 0.0  # mm total clearance width (0 = use fixed passes)
    clear_gaps: bool = False  # auto-detect gaps and mill until fully cleared

    # Drilling
    peck_depth: float = 0.5  # mm per peck
    retract_height: float = 1.0  # mm above surface for peck retract
    drill_feed: float = 100.0  # mm/min

    # Outline
    tab_width: float = 2.0  # mm
    tab_height: float = 0.5  # mm (how much material left in tab)
    num_tabs: int = 4

    # Spindle
    spindle_dwell: float = 3.0  # seconds to wait for spindle spinup

    # Z speed limit
    z_max_feed: float = 120.0  # mm/min (2 mm/s) – caps all G0 Z moves to G1 Z at this rate

    # Work coordinate system
    wcs: str = "G54"  # G54-G59

    # UV Mask Exposure
    uv_offset_x: float = 0.0      # mm LED offset from spindle in X
    uv_offset_y: float = 0.0      # mm LED offset from spindle in Y
    uv_spot_size: float = 0.5     # mm UV spot diameter (= scan line spacing)
    uv_expose_speed: float = 200.0  # mm/min
    uv_expose_z: float = 5.0      # mm above board during exposure
    uv_power: int = 255           # S-value for M3 command (LED power)

    @property
    def effective_width(self) -> float:
        """Effective cutting width considering V-bit geometry and depth."""
        if self.isolation_width > 0:
            return self.isolation_width
        # For V-bit: width = 2 * depth * tan(angle/2) + tip_diameter
        half_angle = math.radians(self.tool_angle / 2)
        return 2 * self.cut_depth * math.tan(half_angle) + self.tool_diameter

    def z_move(self, z: float, comment: str = "") -> str:
        """Generate a rate-limited Z move (G1 instead of G0) to avoid lost steps."""
        cmd = f"G1 Z{z:.3f} F{self.z_max_feed:.0f}"
        if comment:
            cmd += f" ; {comment}"
        return cmd


def generate_isolation_gcode(
    copper_geometry: MultiPolygon,
    settings: ToolpathSettings,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> str:
    """
    Generate isolation milling G-code from copper geometry.

    Creates toolpaths that trace around copper features to isolate them.
    Uses Shapely's buffer/offset to generate tool center paths.
    """
    # Auto-compute passes from max_width if set
    step_over = (
        settings.pass_overlap if settings.pass_overlap > 0
        else settings.effective_width * 0.5
    )
    if settings.max_width > 0:
        ew = settings.effective_width
        if settings.max_width <= ew:
            settings.passes = 1
        else:
            settings.passes = math.ceil((settings.max_width - ew) / step_over) + 1
        # Safety cap
        settings.passes = min(settings.passes, 200)

    # Dynamic mode: clear_gaps overrides fixed pass count.
    # Check whether the geometry actually has gaps worth clearing.
    dynamic_mode = settings.clear_gaps
    max_dynamic_passes = 200  # safety cap
    MAX_CLEAR_GAPS_OFFSET = 5.0  # mm — practical limit for PCB isolation

    if dynamic_mode:
        polys = list(copper_geometry.geoms) if not copper_geometry.is_empty else []
        has_gaps = len(polys) > 1 or any(
            len(list(p.interiors)) > 0 for p in polys
        )
        if not has_gaps:
            # Single feature with no holes — nothing to clear, just isolate.
            dynamic_mode = False
            settings.passes = 1

    _logger.info(
        f"Generating isolation toolpaths: "
        f"{len(copper_geometry.geoms)} polygons, "
        + (f"clear_gaps mode, " if dynamic_mode else f"{settings.passes} pass(es), ")
        + f"tool={settings.tool_diameter}mm"
        + (f", max_width={settings.max_width}mm" if settings.max_width > 0 else "")
    )

    # Calculate depth passes
    dpp = settings.depth_per_pass
    if dpp > 0 and dpp < settings.cut_depth:
        depth_steps = []
        z = dpp
        while z < settings.cut_depth:
            depth_steps.append(z)
            z += dpp
        depth_steps.append(settings.cut_depth)  # final pass at full depth
    else:
        depth_steps = [settings.cut_depth]

    lines = []
    lines.append("; === Isolation Milling G-code ===")
    lines.append(f"; Tool diameter: {settings.tool_diameter} mm")
    lines.append(f"; V-bit angle: {settings.tool_angle}°")
    lines.append(f"; Effective width: {settings.effective_width:.3f} mm")
    lines.append(f"; Cut depth: {settings.cut_depth} mm")
    lines.append(f"; Depth per pass: {dpp if dpp > 0 else settings.cut_depth} mm ({len(depth_steps)} depth pass(es))")
    lines.append(f"; Feed rate: {settings.feed_rate} mm/min")
    lines.append(f"; Plunge rate: {settings.plunge_rate} mm/min")
    lines.append(f"; Spindle speed: {settings.spindle_speed} RPM")
    lines.append(f"; Lateral passes: {'auto (clear gaps)' if dynamic_mode else settings.passes}")
    lines.append(f"; Pass overlap: {settings.pass_overlap} mm")
    if settings.max_width > 0 and not dynamic_mode:
        lines.append(f"; Clearance width: {settings.max_width} mm (auto {settings.passes} passes)")
    lines.append("")

    # Preamble
    lines.append("G21 ; mm mode")
    lines.append("G90 ; absolute positioning")
    lines.append(f"{settings.wcs} ; work coordinate system")
    lines.append(settings.z_move(settings.safe_z, "safe Z"))
    lines.append(f"M3 S{settings.spindle_speed} ; spindle on")
    lines.append(f"G4 P{settings.spindle_dwell:.1f} ; dwell for spindle spinup")
    lines.append("")

    offset = settings.effective_width / 2
    total_paths = 0
    step_over_val = (
        settings.pass_overlap if settings.pass_overlap > 0
        else settings.effective_width * 0.5
    )

    # ------------------------------------------------------------------
    # Narrow-gap pre-pass: detect gaps between copper features that are
    # narrower than the effective V-bit width but wider than the tool tip.
    # The union-based buffer collapses thin slits in these gaps, so we
    # buffer each polygon individually and cut at a reduced depth that
    # avoids undercutting the adjacent copper.
    # ------------------------------------------------------------------
    half_angle = math.radians(settings.tool_angle / 2)
    min_gap = _find_min_processable_gap(
        copper_geometry, settings.tool_diameter, settings.effective_width
    )
    if min_gap > 0:
        tight_offset = min_gap / 2
        # Maximum depth where groove width equals gap width (no undercut)
        tight_depth = (tight_offset - settings.tool_diameter / 2) / math.tan(
            half_angle
        )
        # Ensure depth is at least enough to cut through copper (~35 µm)
        if tight_depth >= 0.035:
            tight_paths = _buffer_individual_paths(
                copper_geometry, tight_offset
            )
            if tight_paths:
                lines.append(
                    f"; --- Narrow-gap pre-pass (offset {tight_offset:.3f} mm, "
                    f"depth {tight_depth:.3f} mm) ---"
                )
                lines.append(
                    f"; Gap width {min_gap:.3f} mm < effective width "
                    f"{settings.effective_width:.3f} mm"
                )
                _logger.info(
                    f"Narrow-gap pre-pass: {len(tight_paths)} paths at "
                    f"offset {tight_offset:.3f} mm, depth {tight_depth:.3f} mm "
                    f"(min gap {min_gap:.3f} mm)"
                )
                for path_coords in tight_paths:
                    if len(path_coords) < 2:
                        continue
                    total_paths += 1
                    lines.append(f"; Path {total_paths} (narrow-gap)")
                    coords = [
                        (x - origin_x, y - origin_y)
                        for x, y in path_coords
                    ]
                    lines.append(settings.z_move(settings.travel_z))
                    lines.append(
                        f"G0 X{coords[0][0]:.4f} Y{coords[0][1]:.4f}"
                    )
                    lines.append(
                        f"G1 Z{-tight_depth:.4f} F{settings.plunge_rate:.0f}"
                    )
                    for ci, (x, y) in enumerate(coords[1:]):
                        if ci == 0:
                            lines.append(f"G1 X{x:.4f} Y{y:.4f} F{settings.feed_rate:.0f}")
                        else:
                            lines.append(f"G1 X{x:.4f} Y{y:.4f}")
                    if _points_close(coords[0], coords[-1]):
                        lines.append(
                            f"G1 X{coords[0][0]:.4f} Y{coords[0][1]:.4f}"
                        )
                    lines.append(settings.z_move(settings.travel_z))
                    lines.append("")
        else:
            _logger.info(
                f"Narrow gaps found (min {min_gap:.3f} mm) but depth "
                f"{tight_depth:.4f} mm is too shallow — skipping pre-pass"
            )

    # Determine number of passes to run
    if dynamic_mode:
        num_passes = max_dynamic_passes  # will break early when gaps filled
    else:
        num_passes = settings.passes

    actual_passes = 0

    for pass_num in range(num_passes):
        current_offset = offset + pass_num * step_over_val

        # In clear-gaps mode, stop if offset exceeds practical limit
        if dynamic_mode and current_offset > MAX_CLEAR_GAPS_OFFSET:
            _logger.info(
                f"Clear-gaps: reached max offset limit "
                f"({MAX_CLEAR_GAPS_OFFSET} mm) after {actual_passes} pass(es)"
            )
            break

        # Buffer the entire copper union at once — this correctly merges
        # overlapping offset zones between nearby features (pads inside
        # ground plane clearances) so the tool doesn't double-cut.
        paths, n_polys, n_interiors = _buffer_copper_paths(
            copper_geometry, current_offset
        )

        if not paths:
            if dynamic_mode:
                _logger.info(
                    f"Clear-gaps: no more paths at offset "
                    f"{current_offset:.3f} mm after {actual_passes} pass(es)"
                )
            break

        actual_passes += 1
        lines.append(f"; --- Lateral Pass {pass_num + 1} (offset {current_offset:.3f} mm) ---")

        for path_coords in paths:
            if len(path_coords) < 2:
                continue

            total_paths += 1
            lines.append(f"; Path {total_paths}")

            # Apply origin offset
            coords = [
                (x - origin_x, y - origin_y) for x, y in path_coords
            ]

            # Multi-depth pass loop
            for depth_idx, z_depth in enumerate(depth_steps):
                if len(depth_steps) > 1:
                    lines.append(f"; Depth pass {depth_idx + 1}/{len(depth_steps)} (Z={-z_depth:.4f})")

                # Rapid to start
                lines.append(settings.z_move(settings.travel_z))
                lines.append(f"G0 X{coords[0][0]:.4f} Y{coords[0][1]:.4f}")

                # Plunge
                lines.append(f"G1 Z{-z_depth:.4f} F{settings.plunge_rate:.0f}")

                # Cut path
                for ci, (x, y) in enumerate(coords[1:]):
                    if ci == 0:
                        lines.append(f"G1 X{x:.4f} Y{y:.4f} F{settings.feed_rate:.0f}")
                    else:
                        lines.append(f"G1 X{x:.4f} Y{y:.4f}")

                # Close the path if it's a loop (first == last)
                if _points_close(coords[0], coords[-1]):
                    lines.append(f"G1 X{coords[0][0]:.4f} Y{coords[0][1]:.4f}")

            # Retract after all depth passes for this path
            lines.append(settings.z_move(settings.travel_z))
            lines.append("")

        # In clear-gaps mode, stop once all features have merged into a
        # single polygon with no interior holes — all gaps are filled.
        if dynamic_mode and n_polys <= 1 and n_interiors == 0:
            _logger.info(
                f"Clear-gaps: all gaps filled after {actual_passes} pass(es) "
                f"at offset {current_offset:.3f} mm"
            )
            break

    # Postamble
    lines.append("; === End ===")
    lines.append(settings.z_move(settings.safe_z, "safe Z"))
    lines.append("M5 ; spindle off")
    lines.append("G0 X0 Y0 ; return to origin")
    lines.append("M2 ; end of program")

    _logger.info(f"Generated {total_paths} toolpaths ({actual_passes} lateral pass(es)), {len(lines)} lines of G-code")
    return "\n".join(lines)


def _find_min_processable_gap(
    copper: MultiPolygon, tool_diameter: float, effective_width: float
) -> float:
    """
    Find the minimum gap between adjacent copper features that a V-bit
    can still isolate (gap >= tool_diameter but < effective_width).

    Returns 0 if no such narrow gaps exist.
    """
    polys = list(copper.geoms) if not copper.is_empty else []
    if len(polys) < 2:
        return 0.0

    from shapely.strtree import STRtree

    # Use spatial index for efficient neighbour search
    tree = STRtree(polys)
    min_gap = float("inf")

    for i, poly in enumerate(polys):
        # Query candidates whose bounding box is within effective_width
        candidates = tree.query(poly.buffer(effective_width))
        for j in candidates:
            if j <= i:
                continue
            dist = poly.distance(polys[j])
            if tool_diameter <= dist < effective_width:
                min_gap = min(min_gap, dist)

    return min_gap if min_gap < float("inf") else 0.0


def _buffer_individual_paths(
    copper: MultiPolygon, offset: float
) -> list[list[tuple]]:
    """
    Buffer each copper polygon individually and extract toolpath rings.

    Unlike `_buffer_copper_paths` this does **not** union the results,
    so thin slits between adjacent buffered features are preserved as
    separate exterior rings.  This is essential for narrow-gap isolation
    where the union-based approach collapses near-zero-width slits.
    """
    paths: list[list[tuple]] = []

    polys = (
        list(copper.geoms) if isinstance(copper, MultiPolygon) else [copper]
    )

    for poly in polys:
        buffered = poly.buffer(offset, join_style=2, quad_segs=16)
        if buffered.is_empty:
            continue
        if isinstance(buffered, Polygon):
            ext = list(buffered.exterior.coords)
            if len(ext) >= 3:
                paths.append(ext)
            for interior in buffered.interiors:
                ring = list(interior.coords)
                if len(ring) >= 3:
                    paths.append(ring)
        elif isinstance(buffered, MultiPolygon):
            for p in buffered.geoms:
                ext = list(p.exterior.coords)
                if len(ext) >= 3:
                    paths.append(ext)
                for interior in p.interiors:
                    ring = list(interior.coords)
                    if len(ring) >= 3:
                        paths.append(ring)

    return paths


def _buffer_copper_paths(
    copper: MultiPolygon, offset: float
) -> tuple[list[list[tuple]], int, int]:
    """
    Generate isolation toolpaths by buffering the entire copper geometry.

    Uses Shapely ``buffer()`` on the whole MultiPolygon at once so that
    overlapping offset zones between nearby features (e.g. a pad sitting
    inside a ground-plane clearance hole) merge into a single path instead
    of producing duplicate/overlapping cuts.

    Returns ``(paths, num_polygons, num_interiors)`` where *paths* is a list
    of coordinate-ring lists, *num_polygons* is the polygon count in the
    buffered result, and *num_interiors* is the total interior-ring count.
    These topology counters let callers detect when gap merging has
    stabilised (used by clear-gaps mode).
    """
    buffered = copper.buffer(offset, join_style=2, quad_segs=16)

    if buffered.is_empty:
        return [], 0, 0

    paths: list[list[tuple]] = []
    num_polygons = 0
    num_interiors = 0

    def _extract_polygon_rings(poly: Polygon):
        nonlocal num_polygons, num_interiors
        if poly.is_empty:
            return
        num_polygons += 1
        num_interiors += len(list(poly.interiors))
        ext = list(poly.exterior.coords)
        if len(ext) >= 3:
            paths.append(ext)
        for interior in poly.interiors:
            ring = list(interior.coords)
            if len(ring) >= 3:
                paths.append(ring)

    if isinstance(buffered, Polygon):
        _extract_polygon_rings(buffered)
    elif isinstance(buffered, MultiPolygon):
        for p in buffered.geoms:
            _extract_polygon_rings(p)
    elif hasattr(buffered, "geoms"):
        # GeometryCollection fallback
        for g in buffered.geoms:
            if isinstance(g, Polygon):
                _extract_polygon_rings(g)

    return paths, num_polygons, num_interiors


def _points_close(p1: tuple, p2: tuple, tol: float = 0.001) -> bool:
    """Check if two points are approximately equal."""
    return abs(p1[0] - p2[0]) < tol and abs(p1[1] - p2[1]) < tol


# =============================================================================
# Drilling G-code
# =============================================================================


def generate_drill_gcode(
    drills: list,
    settings: ToolpathSettings,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> str:
    """
    Generate drilling G-code from drill info list.

    Supports peck drilling for deeper holes. Groups by tool for efficient
    tool changes.
    """
    _logger.info(f"Generating drill G-code: {len(drills)} tools")

    lines = []
    lines.append("; === Drilling G-code ===")
    lines.append(f"; Drill feed: {settings.drill_feed} mm/min")
    lines.append(f"; Spindle speed: {settings.spindle_speed} RPM")
    lines.append("")

    # Preamble
    lines.append("G21 ; mm mode")
    lines.append("G90 ; absolute positioning")
    lines.append(f"{settings.wcs} ; work coordinate system")
    lines.append(settings.z_move(settings.safe_z, "safe Z"))
    lines.append(f"M3 S{settings.spindle_speed} ; spindle on")
    lines.append(f"G4 P{settings.spindle_dwell:.1f} ; dwell for spindle spinup")
    lines.append("")

    total_holes = 0

    for drill in drills:
        if not drill.holes:
            continue

        lines.append(f"; --- Tool T{drill.tool_number}: {drill.diameter:.2f} mm ({drill.count} holes) ---")
        lines.append(f"M0 ; Pause for tool change: {drill.diameter:.2f} mm drill")
        lines.append(f"M3 S{settings.spindle_speed}")
        lines.append(f"G4 P{settings.spindle_dwell:.1f}")
        lines.append("")

        # Optimize hole order (nearest-neighbor)
        ordered_holes = _optimize_drill_order(drill.holes)

        for x, y in ordered_holes:
            total_holes += 1
            hx = x - origin_x
            hy = y - origin_y

            # Rapid to hole position
            lines.append(f"G0 X{hx:.4f} Y{hy:.4f}")

            # Determine drill depth from board thickness (use cut_depth as total)
            depth = settings.cut_depth
            if depth <= settings.peck_depth or settings.peck_depth <= 0:
                # Simple drill cycle
                lines.append(settings.z_move(settings.retract_height))
                lines.append(f"G1 Z{-depth:.4f} F{settings.drill_feed:.0f}")
                lines.append(settings.z_move(settings.travel_z))
            else:
                # Peck drilling
                current_depth = 0.0
                lines.append(settings.z_move(settings.retract_height))
                while current_depth < depth:
                    current_depth = min(current_depth + settings.peck_depth, depth)
                    lines.append(f"G1 Z{-current_depth:.4f} F{settings.drill_feed:.0f}")
                    lines.append(settings.z_move(settings.retract_height, "retract"))
                lines.append(settings.z_move(settings.travel_z))

        lines.append("")

    # Postamble
    lines.append("; === End ===")
    lines.append(settings.z_move(settings.safe_z, "safe Z"))
    lines.append("M5 ; spindle off")
    lines.append("G0 X0 Y0 ; return to origin")
    lines.append("M2 ; end of program")

    _logger.info(f"Generated drill G-code for {total_holes} holes")
    return "\n".join(lines)


def _optimize_drill_order(holes: list[tuple]) -> list[tuple]:
    """
    Optimize drill order using nearest-neighbor heuristic.
    Minimizes total travel distance between holes.
    """
    if len(holes) <= 2:
        return list(holes)

    remaining = list(holes)
    ordered = [remaining.pop(0)]

    while remaining:
        last = ordered[-1]
        nearest_idx = 0
        nearest_dist = float("inf")

        for i, hole in enumerate(remaining):
            dist = (hole[0] - last[0]) ** 2 + (hole[1] - last[1]) ** 2
            if dist < nearest_dist:
                nearest_dist = dist
                nearest_idx = i

        ordered.append(remaining.pop(nearest_idx))

    return ordered


# =============================================================================
# Outline Cutting G-code
# =============================================================================


def generate_outline_gcode(
    outline_geometry,
    settings: ToolpathSettings,
    board_thickness: float = 1.6,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> str:
    """
    Generate outline/profile cutting G-code.

    Cuts the board outline with optional holding tabs. The tool path
    is offset outward by half the tool diameter.
    """
    _logger.info("Generating outline G-code")

    lines = []
    lines.append("; === Outline Cutting G-code ===")
    lines.append(f"; Board thickness: {board_thickness} mm")
    lines.append(f"; Tool diameter: {settings.tool_diameter} mm")
    lines.append(f"; Tab width: {settings.tab_width} mm")
    lines.append(f"; Tabs: {settings.num_tabs}")
    lines.append("")

    # Preamble
    lines.append("G21 ; mm mode")
    lines.append("G90 ; absolute positioning")
    lines.append(f"{settings.wcs} ; work coordinate system")
    lines.append(settings.z_move(settings.safe_z))
    lines.append(f"M3 S{settings.spindle_speed}")
    lines.append(f"G4 P{settings.spindle_dwell:.1f}")
    lines.append("")

    # Get outline polygon and apply tool offset (outward by half tool diameter)
    offset = settings.tool_diameter / 2

    if isinstance(outline_geometry, Polygon):
        base_poly = outline_geometry
    elif isinstance(outline_geometry, MultiPolygon):
        # Use the largest polygon
        base_poly = max(outline_geometry.geoms, key=lambda p: p.area)
    elif isinstance(outline_geometry, (LineString, LinearRing)):
        # Convert line/ring to polygon, then offset
        try:
            base_poly = Polygon(outline_geometry.coords)
        except (ValueError, TypeError) as e:
            _logger.error(f"Cannot form polygon from outline line geometry: {e}")
            return ""
    else:
        _logger.error(f"Unsupported outline geometry type: {type(outline_geometry)}")
        return ""

    # Buffer outward to offset tool center outside the board edge
    if offset > 0:
        offset_poly = base_poly.buffer(offset, join_style=2)  # mitre join
        exterior_coords = list(offset_poly.exterior.coords)
        _logger.info(
            f"Outline offset: tool_diameter={settings.tool_diameter}, "
            f"offset={offset:.3f}, "
            f"original vertices={len(list(base_poly.exterior.coords))}, "
            f"offset vertices={len(exterior_coords)}"
        )
    else:
        exterior_coords = list(base_poly.exterior.coords)

    # Generate tab positions along the outline
    tab_positions = _generate_tab_positions(
        exterior_coords, settings.num_tabs, settings.tab_width
    )

    # Multi-pass depth cutting
    num_depth_passes = max(1, math.ceil(board_thickness / settings.peck_depth))

    for depth_pass in range(num_depth_passes):
        current_depth = min(
            (depth_pass + 1) * settings.peck_depth, board_thickness
        )
        lines.append(f"; Depth pass {depth_pass + 1}: Z{-current_depth:.3f}")

        # Apply origin offset
        coords = [(x - origin_x, y - origin_y) for x, y in exterior_coords]

        # Rapid to start
        lines.append(settings.z_move(settings.travel_z))
        lines.append(f"G0 X{coords[0][0]:.4f} Y{coords[0][1]:.4f}")
        lines.append(f"G1 Z{-current_depth:.4f} F{settings.plunge_rate:.0f}")

        # Cut outline with tabs (first move sets feed rate)
        accumulated_dist = 0.0
        feed_set = False
        for i in range(1, len(coords)):
            x, y = coords[i]
            prev_x, prev_y = coords[i - 1]
            seg_len = math.sqrt((x - prev_x) ** 2 + (y - prev_y) ** 2)

            # Check if this segment crosses a tab position
            in_tab = False
            for tab_start, tab_end in tab_positions:
                if (
                    accumulated_dist + seg_len > tab_start
                    and accumulated_dist < tab_end
                ):
                    in_tab = True
                    break

            if in_tab and current_depth > (board_thickness - settings.tab_height):
                # Raise Z for tab
                tab_z = -(board_thickness - settings.tab_height)
                lines.append(f"G1 Z{tab_z:.4f} F{settings.plunge_rate:.0f}")
                lines.append(f"G1 X{x:.4f} Y{y:.4f} F{settings.feed_rate:.0f}")
                lines.append(f"G1 Z{-current_depth:.4f} F{settings.plunge_rate:.0f}")
                feed_set = True
            else:
                if not feed_set:
                    lines.append(f"G1 X{x:.4f} Y{y:.4f} F{settings.feed_rate:.0f}")
                    feed_set = True
                else:
                    lines.append(f"G1 X{x:.4f} Y{y:.4f}")

            accumulated_dist += seg_len

        # Retract
        lines.append(settings.z_move(settings.travel_z))
        lines.append("")

    # Postamble
    lines.append(settings.z_move(settings.safe_z))
    lines.append("M5")
    lines.append("G0 X0 Y0")
    lines.append("M2")

    return "\n".join(lines)


def _generate_tab_positions(
    coords: list[tuple], num_tabs: int, tab_width: float
) -> list[tuple]:
    """
    Generate tab positions along the outline path.
    Returns list of (start_distance, end_distance) tuples.
    """
    # Calculate total perimeter
    total_length = 0.0
    for i in range(1, len(coords)):
        dx = coords[i][0] - coords[i - 1][0]
        dy = coords[i][1] - coords[i - 1][1]
        total_length += math.sqrt(dx * dx + dy * dy)

    if num_tabs <= 0 or total_length <= 0:
        return []

    spacing = total_length / num_tabs
    tabs = []
    for i in range(num_tabs):
        center = spacing * (i + 0.5)
        tabs.append((center - tab_width / 2, center + tab_width / 2))

    return tabs


# =============================================================================
# UV Solder Mask Exposure G-code
# =============================================================================


def generate_mask_expose_gcode(
    mask_geometry,
    board_bounds: tuple[float, float, float, float],
    settings: ToolpathSettings,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> str:
    """
    Generate UV solder mask exposure G-code using raster fill.

    The mask Gerber defines pad openings — areas where the solder mask is
    removed.  The UV LED must cure everything *else* (board minus openings).

    A serpentine raster scan fills the keep-mask area while the LED is on;
    rapid moves with the LED off skip over pad openings.

    All XY coordinates are shifted by the UV LED offset so the CNC moves
    the spindle to a position where the physically offset LED is over the
    target area.

    Args:
        mask_geometry: Merged Shapely geometry of mask openings (pad
            exposures).  ``None`` or empty → expose entire board.
        board_bounds: Design-coordinate bounding box ``(min_x, min_y,
            max_x, max_y)``.
        settings: ToolpathSettings with UV fields populated.
        origin_x / origin_y: Design-coordinate origin subtracted from
            all output coordinates so G-code is relative to (0, 0).

    Returns:
        Multi-line G-code string, or ``""`` if there is nothing to expose.
    """
    bx0, by0, bx1, by1 = board_bounds
    board_poly = box(bx0, by0, bx1, by1)

    # keep_area = board minus mask openings
    if mask_geometry is not None and not mask_geometry.is_empty:
        keep_area = board_poly.difference(mask_geometry)
    else:
        keep_area = board_poly

    if keep_area.is_empty:
        _logger.warning("Mask exposure: keep area is empty — nothing to expose")
        return ""

    spot = settings.uv_spot_size
    if spot <= 0:
        spot = 0.5

    # Erode the keep area by half the spot size so the beam edge does not
    # bleed into pad openings.  The scan line tracks the beam *center*, so
    # shrinking the allowed area by the beam radius keeps the full circular
    # spot inside the mask.
    keep_area = keep_area.buffer(-spot / 2)
    if keep_area.is_empty:
        _logger.warning(
            "Mask exposure: keep area empty after beam-radius erosion "
            f"(spot={spot} mm) — nothing to expose"
        )
        return ""

    _logger.info(
        "Generating mask exposure G-code: "
        f"board={bx1 - bx0:.1f}x{by1 - by0:.1f} mm, "
        f"spot={spot} mm, speed={settings.uv_expose_speed} mm/min, "
        f"LED offset=({settings.uv_offset_x}, {settings.uv_offset_y})"
    )

    lines: list[str] = []
    lines.append("; === UV Solder Mask Exposure G-code ===")
    lines.append(f"; Board: {bx1 - bx0:.2f} x {by1 - by0:.2f} mm")
    lines.append(f"; UV spot size: {spot} mm")
    lines.append(f"; Exposure speed: {settings.uv_expose_speed} mm/min")
    lines.append(f"; Exposure Z: {settings.uv_expose_z} mm")
    lines.append(f"; LED offset: X={settings.uv_offset_x} Y={settings.uv_offset_y} mm")
    lines.append(f"; LED power: S{settings.uv_power}")
    lines.append("; NOTE: M3/M5 control the UV LED, NOT the spindle.")
    lines.append("")

    # Preamble
    lines.append("G21 ; mm mode")
    lines.append("G90 ; absolute positioning")
    lines.append(f"{settings.wcs} ; work coordinate system")
    lines.append(settings.z_move(settings.safe_z, "safe Z"))
    lines.append("M5 ; LED off")
    lines.append(settings.z_move(settings.uv_expose_z, "exposure height"))
    lines.append("")

    # LED offset compensation: to position the LED at (tx, ty),
    # command the CNC to (tx - offset_x, ty - offset_y).
    dx = settings.uv_offset_x
    dy = settings.uv_offset_y

    # Raster scan bounds (in design coordinates)
    ka = keep_area.bounds  # (minx, miny, maxx, maxy)
    scan_y_start = ka[1] + spot / 2
    scan_y_end = ka[3]
    scan_x_min = ka[0] - 1  # extend for clean intersection
    scan_x_max = ka[2] + 1

    y = scan_y_start
    scan_idx = 0
    total_segments = 0

    while y <= scan_y_end:
        scan_line = LineString([(scan_x_min, y), (scan_x_max, y)])
        intersection = keep_area.intersection(scan_line)

        segments = _extract_line_segments(intersection)
        if segments:
            # Sort by start-X ascending
            segments.sort(key=lambda s: s[0][0])

            # Serpentine: reverse on odd rows
            if scan_idx % 2 == 1:
                segments = [list(reversed(seg)) for seg in reversed(segments)]

            for seg_coords in segments:
                total_segments += 1
                # Transform: subtract origin, subtract LED offset
                transformed = [
                    (x - origin_x - dx, yc - origin_y - dy)
                    for x, yc in seg_coords
                ]
                # Rapid to start (LED off)
                lines.append(
                    f"G0 X{transformed[0][0]:.4f} Y{transformed[0][1]:.4f}"
                )
                # LED on
                lines.append(f"M3 S{settings.uv_power}")
                # Expose along segment
                for cx, cy in transformed[1:]:
                    lines.append(
                        f"G1 X{cx:.4f} Y{cy:.4f} F{settings.uv_expose_speed:.0f}"
                    )
                # LED off
                lines.append("M5")

        y += spot
        scan_idx += 1

    # Postamble
    lines.append("")
    lines.append("; === End ===")
    lines.append("M5 ; LED off")
    lines.append(settings.z_move(settings.safe_z, "safe Z"))
    lines.append("G0 X0 Y0 ; return to origin")
    lines.append("M2 ; end of program")

    _logger.info(
        f"Generated mask exposure G-code: {total_segments} segments, "
        f"{len(lines)} lines"
    )
    return "\n".join(lines)


def _extract_line_segments(geometry) -> list[list[tuple]]:
    """Extract LineString coordinate lists from an intersection result."""
    segments: list[list[tuple]] = []
    if geometry.is_empty:
        return segments
    if isinstance(geometry, LineString):
        coords = list(geometry.coords)
        if len(coords) >= 2:
            segments.append(coords)
    elif isinstance(geometry, MultiLineString):
        for geom in geometry.geoms:
            coords = list(geom.coords)
            if len(coords) >= 2:
                segments.append(coords)
    elif hasattr(geometry, "geoms"):
        # GeometryCollection — pick only LineStrings
        for geom in geometry.geoms:
            if isinstance(geom, LineString):
                coords = list(geom.coords)
                if len(coords) >= 2:
                    segments.append(coords)
    return segments


# =============================================================================
# Double-sided board support
# =============================================================================


def mirror_coordinates_x(
    gcode: str, board_width: float, flip_offset: float = 0.0
) -> str:
    """
    Mirror G-code X coordinates for bottom-side milling.

    Flips X coordinates: new_x = board_width - old_x + flip_offset
    Used when the board is flipped along the Y axis.
    """
    import re

    _logger.info(f"Mirroring G-code X coordinates (board_width={board_width})")

    x_pattern = re.compile(r"X([+-]?\d+\.?\d*)")

    def replace_x(match):
        old_x = float(match.group(1))
        new_x = board_width - old_x + flip_offset
        return f"X{new_x:.4f}"

    mirrored_lines = []
    for line in gcode.splitlines():
        if line.startswith(";") or not line.strip():
            mirrored_lines.append(line)
            continue
        mirrored_lines.append(x_pattern.sub(replace_x, line))

    return "\n".join(mirrored_lines)


def mirror_coordinates_y(
    gcode: str, board_height: float, flip_offset: float = 0.0
) -> str:
    """Mirror G-code Y coordinates for bottom-side milling (flip along X axis)."""
    import re

    y_pattern = re.compile(r"Y([+-]?\d+\.?\d*)")

    def replace_y(match):
        old_y = float(match.group(1))
        new_y = board_height - old_y + flip_offset
        return f"Y{new_y:.4f}"

    mirrored_lines = []
    for line in gcode.splitlines():
        if line.startswith(";") or not line.strip():
            mirrored_lines.append(line)
            continue
        mirrored_lines.append(y_pattern.sub(replace_y, line))

    return "\n".join(mirrored_lines)


# =============================================================================
# Alignment compensation for double-sided boards
# =============================================================================


@dataclass
class AlignmentTransform:
    """Rigid-body 2D transform: rotation + translation.

    Maps expected (design) coordinates to measured (actual) coordinates:
        x' = cos_theta * x - sin_theta * y + tx
        y' = sin_theta * x + cos_theta * y + ty
    """
    cos_theta: float = 1.0
    sin_theta: float = 0.0
    tx: float = 0.0
    ty: float = 0.0
    theta_deg: float = 0.0  # rotation in degrees (for display)
    rms_error: float = 0.0  # mm RMS fit error (for display)

    def to_dict(self) -> dict:
        return {
            "cos_theta": self.cos_theta,
            "sin_theta": self.sin_theta,
            "tx": self.tx,
            "ty": self.ty,
            "theta_deg": self.theta_deg,
            "rms_error": self.rms_error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AlignmentTransform":
        return cls(**{k: v for k, v in data.items()
                      if k in ("cos_theta", "sin_theta", "tx", "ty",
                               "theta_deg", "rms_error")})


def compute_alignment_transform(
    expected: list[tuple[float, float]],
    measured: list[tuple[float, float]],
) -> AlignmentTransform:
    """Compute rigid-body transform from expected→measured point pairs.

    Uses least-squares to solve for rotation + translation.
    Requires at least 2 point pairs.

    The system for each point pair (e, m):
        a * ex - b * ey + tx = mx
        b * ex + a * ey + ty = my
    where a = cos(θ), b = sin(θ).

    We solve [a, b, tx, ty] via least-squares, then normalise (a,b)
    to enforce a pure rotation (no scaling).
    """
    import numpy as np

    n = len(expected)
    if n < 2:
        raise ValueError("Need at least 2 alignment points")
    if n != len(measured):
        raise ValueError("Expected and measured point lists must have same length")

    # Build the linear system  A @ [a, b, tx, ty]^T = rhs
    A = np.zeros((2 * n, 4))
    rhs = np.zeros(2 * n)

    for i in range(n):
        ex, ey = expected[i]
        mx, my = measured[i]
        row = 2 * i
        # a*ex - b*ey + tx = mx
        A[row] = [ex, -ey, 1.0, 0.0]
        rhs[row] = mx
        # b*ex + a*ey + ty = my
        A[row + 1] = [ey, ex, 0.0, 1.0]
        rhs[row + 1] = my

    result, _, _, _ = np.linalg.lstsq(A, rhs, rcond=None)
    a, b, tx, ty = result

    # Normalise to pure rotation (remove any scaling component)
    scale = math.sqrt(a * a + b * b)
    if scale > 1e-9:
        a /= scale
        b /= scale
    else:
        a, b = 1.0, 0.0

    theta_deg = math.degrees(math.atan2(b, a))

    # Compute RMS error
    total_sq = 0.0
    for i in range(n):
        ex, ey = expected[i]
        mx, my = measured[i]
        px = a * ex - b * ey + tx
        py = b * ex + a * ey + ty
        total_sq += (px - mx) ** 2 + (py - my) ** 2
    rms_error = math.sqrt(total_sq / n)

    _logger.info(
        f"Alignment transform: θ={theta_deg:.3f}° "
        f"tx={tx:.4f} ty={ty:.4f} RMS={rms_error:.4f}mm"
    )

    return AlignmentTransform(
        cos_theta=a, sin_theta=b,
        tx=tx, ty=ty,
        theta_deg=theta_deg,
        rms_error=rms_error,
    )


def apply_alignment_transform(gcode: str, transform: AlignmentTransform) -> str:
    """Apply a rigid-body alignment transform to all XY coordinates in G-code.

    Rewrites every X and Y value on G0/G1 lines:
        x' = cos_θ · x − sin_θ · y + tx
        y' = sin_θ · x + cos_θ · y + ty
    """
    a = transform.cos_theta
    b = transform.sin_theta
    tx = transform.tx
    ty = transform.ty

    # If the transform is essentially identity, skip the work
    if abs(b) < 1e-7 and abs(tx) < 1e-5 and abs(ty) < 1e-5 and abs(a - 1.0) < 1e-7:
        return gcode

    x_pat = re.compile(r"X([+-]?\d+\.?\d*)")
    y_pat = re.compile(r"Y([+-]?\d+\.?\d*)")

    output = []
    for line in gcode.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            output.append(line)
            continue

        # Only transform lines that have both X and Y (movement commands)
        x_match = x_pat.search(stripped)
        y_match = y_pat.search(stripped)

        if x_match and y_match:
            old_x = float(x_match.group(1))
            old_y = float(y_match.group(1))
            new_x = a * old_x - b * old_y + tx
            new_y = b * old_x + a * old_y + ty
            line = x_pat.sub(f"X{new_x:.4f}", line)
            line = y_pat.sub(f"Y{new_y:.4f}", line)
        elif x_match and not y_match:
            # X-only line: we still need to transform but Y is implicit
            # (carried from previous position). Pass through unchanged —
            # the caller should ensure full XY is present via subdivision.
            pass

        output.append(line)

    return "\n".join(output)
