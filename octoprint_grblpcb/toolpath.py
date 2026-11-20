"""
Toolpath Generation

Generates G-code toolpaths from parsed Gerber/Excellon geometries:
- Isolation milling: offset copper geometry outward to create isolation trenches
- Drilling: generate drill G-code with peck drilling support
- Outline cutting: cut board outline with optional tabs
- Double-sided alignment: compute and apply coordinate transforms
"""

import itertools
import logging
import math
import re
from dataclasses import dataclass

from shapely import affinity
from shapely.geometry import (
    LinearRing,
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
    box,
)
from shapely.geometry.polygon import orient

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
    pass_overlap: float = 40.0  # % of the trench width that neighbouring passes share
    isolation_width: float = 0.0  # if > 0, overrides tool_diameter for offset
    depth_per_pass: float = 0.0  # mm per depth pass (0 = full depth in one pass)
    max_width: float = 0.0  # mm total clearance width (0 = use fixed passes)
    clear_gaps: bool = False  # auto-detect gaps and mill until fully cleared

    # Drilling
    peck_depth: float = 0.5  # mm per peck
    retract_height: float = 1.0  # mm above surface for peck retract
    drill_feed: float = 100.0  # mm/min
    # Drilling and outline go this far below the board so no skin is left
    # (board tolerance, uneven spoil board, drill tip cone)
    breakthrough_depth: float = 0.2  # mm

    # Outline
    outline_step_down: float = 0.5  # mm depth per outline pass
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
    uv_line_overlap: float = 20.0  # % of the spot that neighbouring scan lines share

    @property
    def effective_width(self) -> float:
        """Effective cutting width considering V-bit geometry and depth."""
        if self.isolation_width > 0:
            return self.isolation_width
        # For V-bit: width = 2 * depth * tan(angle/2) + tip_diameter
        half_angle = math.radians(self.tool_angle / 2)
        return 2 * self.cut_depth * math.tan(half_angle) + self.tool_diameter

    @property
    def step_over(self) -> float:
        """Distance between neighbouring isolation passes (always overlapping)."""
        overlap = min(max(self.pass_overlap, 10.0), 90.0) / 100.0
        return self.effective_width * (1.0 - overlap)

    def z_move(self, z: float, comment: str = "") -> str:
        """Generate a rate-limited Z move (G1 instead of G0) to avoid lost steps."""
        cmd = f"G1 Z{z:.3f} F{self.z_max_feed:.0f}"
        if comment:
            cmd += f" ; {comment}"
        return cmd


# Milling direction: with a clockwise spindle (M3) the cutter edge moves
# against the feed on the side of the finished wall, so climb milling keeps
# the material that stays (copper, board) on the RIGHT of the direction of
# travel. shapely's orient(sign=-1) gives exteriors clockwise and holes
# counter-clockwise, which is exactly that for every ring of a "material"
# polygon: rings around copper and the outer board contour run clockwise,
# holes and cutouts counter-clockwise.
CLIMB_SIGN = -1.0
CLIMB_NOTE = "; Direction: climb milling (M3), the remaining material on the right"


def _rings(geometry) -> list[list[tuple]]:
    """Closed rings of a (Multi)Polygon, oriented for climb milling."""
    polys = []
    if isinstance(geometry, Polygon):
        polys = [geometry]
    elif hasattr(geometry, "geoms"):
        polys = [g for g in geometry.geoms if isinstance(g, Polygon)]
    rings = []
    for poly in polys:
        if poly.is_empty:
            continue
        poly = orient(poly, sign=CLIMB_SIGN)
        for ring in [poly.exterior, *poly.interiors]:
            coords = list(ring.coords)
            if len(coords) >= 4:
                rings.append(coords)
    return rings


def _order_rings(rings: list[list[tuple]], start=(0.0, 0.0)) -> list[list[tuple]]:
    """
    Visit closed rings nearest-first and start each at the vertex closest
    to where the tool is, to cut down travel moves.

    All vertices live in one numpy array, so each step is a single vector
    operation: boards with thousands of contours order in seconds instead
    of minutes.
    """
    import numpy as np

    remaining = [r[:-1] if r[0] == r[-1] else list(r) for r in rings]
    remaining = [r for r in remaining if r]
    if not remaining:
        return []
    points = np.array([p[:2] for ring in remaining for p in ring], dtype=float)
    lengths = [len(ring) for ring in remaining]
    offsets = np.concatenate(([0], np.cumsum(lengths)))
    owner = np.repeat(np.arange(len(remaining)), lengths)
    open_vertex = np.ones(len(points), dtype=bool)

    ordered = []
    pos = start
    for _ in range(len(remaining)):
        dist = (points[:, 0] - pos[0]) ** 2 + (points[:, 1] - pos[1]) ** 2
        dist[~open_vertex] = np.inf
        nearest = int(np.argmin(dist))
        ri = int(owner[nearest])
        vi = nearest - int(offsets[ri])
        ring = remaining[ri]
        ring = ring[vi:] + ring[:vi]
        ring.append(ring[0])
        ordered.append(ring)
        open_vertex[offsets[ri]:offsets[ri + 1]] = False
        pos = ring[0]
    return ordered


def mirror_geometry(geometry, axis: str, origin: tuple, size: tuple):
    """
    Mirror design geometry for the back side.

    The G-code places design point p at p - origin; after flipping the board
    around the Y axis (axis "X") it is milled at width - (x - origin_x).
    Mirroring the geometry itself (instead of the G-code text) keeps the
    milling direction, because rings are oriented after mirroring.
    """
    if axis == "X":
        return affinity.scale(geometry, xfact=-1, yfact=1, origin=(origin[0] + size[0] / 2, 0))
    return affinity.scale(geometry, xfact=1, yfact=-1, origin=(0, origin[1] + size[1] / 2))


def mirror_point(x: float, y: float, axis: str, origin: tuple, size: tuple) -> tuple:
    """Mirror one design point like mirror_geometry."""
    if axis == "X":
        return 2 * origin[0] + size[0] - x, y
    return x, 2 * origin[1] + size[1] - y


def find_unisolatable_gaps(copper, tool_diameter: float) -> list[tuple]:
    """
    Gaps between separate copper areas that are narrower than the tool tip.

    The tool cannot cut there, so the areas stay connected (a short).
    Returns [(gap_mm, (x, y)), ...] with the location of the narrowest spot.
    """
    from shapely.ops import nearest_points
    from shapely.strtree import STRtree

    polys = list(copper.geoms) if hasattr(copper, "geoms") else [copper]
    if len(polys) < 2:
        return []
    tree = STRtree(polys)
    found = []
    for i, poly in enumerate(polys):
        for j in tree.query(poly.buffer(tool_diameter)):
            if j <= i:
                continue
            dist = poly.distance(polys[j])
            if dist < tool_diameter:
                a, b = nearest_points(poly, polys[j])
                found.append((dist, ((a.x + b.x) / 2, (a.y + b.y) / 2)))
    return sorted(found)


def generate_isolation_gcode(
    copper_geometry: MultiPolygon,
    settings: ToolpathSettings,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
    mirror: str | None = None,
    board_size: tuple = (0.0, 0.0),
) -> str:
    """
    Generate isolation milling G-code from copper geometry.

    Tool centre paths are offsets of the copper outline: the first pass at
    half the trench width (the trench edge touches the copper), further
    passes step outwards by ``settings.step_over`` so neighbouring trenches
    always overlap. ``mirror`` ("X"/"Y") mills the back side of a flipped
    board; see mirror_geometry.
    """
    if mirror:
        copper_geometry = mirror_geometry(copper_geometry, mirror, (origin_x, origin_y), board_size)
        if isinstance(copper_geometry, Polygon):
            copper_geometry = MultiPolygon([copper_geometry])

    ew = settings.effective_width
    step_over = settings.step_over
    if settings.max_width > 0:
        if settings.max_width <= ew:
            settings.passes = 1
        else:
            settings.passes = math.ceil((settings.max_width - ew) / step_over) + 1
        settings.passes = min(settings.passes, 200)  # safety cap

    # Dynamic mode: clear_gaps overrides the fixed pass count
    dynamic_mode = settings.clear_gaps
    max_dynamic_passes = 200  # safety cap
    MAX_CLEAR_GAPS_OFFSET = 5.0  # mm, practical limit for PCB isolation
    if dynamic_mode:
        polys = list(copper_geometry.geoms) if not copper_geometry.is_empty else []
        if not (len(polys) > 1 or any(len(p.interiors) > 0 for p in polys)):
            dynamic_mode = False
            settings.passes = 1

    # Depth steps
    dpp = settings.depth_per_pass
    if 0 < dpp < settings.cut_depth:
        depth_steps = []
        z = dpp
        while z < settings.cut_depth - 1e-9:
            depth_steps.append(z)
            z += dpp
        depth_steps.append(settings.cut_depth)
    else:
        depth_steps = [settings.cut_depth]

    gaps = find_unisolatable_gaps(copper_geometry, settings.tool_diameter)
    narrow_classes, uncut_gaps = plan_narrow_gap_passes(copper_geometry, settings)

    lines = []
    lines.append("; === Isolation Milling G-code ===")
    if mirror:
        lines.append(f"; Back side: mirrored in {mirror} (board flipped)")
    lines.append(f"; Tool diameter: {settings.tool_diameter} mm")
    lines.append(f"; V-bit angle: {settings.tool_angle}°")
    lines.append(f"; Effective width: {ew:.3f} mm")
    lines.append(f"; Cut depth: {settings.cut_depth} mm")
    lines.append(f"; Depth per pass: {dpp if dpp > 0 else settings.cut_depth} mm ({len(depth_steps)} depth pass(es))")
    lines.append(f"; Feed rate: {settings.feed_rate} mm/min")
    lines.append(f"; Plunge rate: {settings.plunge_rate} mm/min")
    lines.append(f"; Spindle speed: {settings.spindle_speed} RPM")
    lines.append(f"; Lateral passes: {'auto (clear gaps)' if dynamic_mode else settings.passes}")
    lines.append(f"; Pass overlap: {min(max(settings.pass_overlap, 10.0), 90.0):.0f} % (step {step_over:.3f} mm)")
    if settings.max_width > 0 and not dynamic_mode:
        lines.append(f"; Clearance width: {settings.max_width} mm (auto {settings.passes} passes)")
    lines.append(CLIMB_NOTE)
    for gap, (gx, gy) in gaps:
        lines.append(
            f"; WARNING: gap {gap:.3f} mm < tool tip {settings.tool_diameter} mm at "
            f"X{gx - origin_x:.2f} Y{gy - origin_y:.2f}: not isolated, copper stays connected"
        )
    for gap, (gx, gy) in uncut_gaps:
        lines.append(
            f"; WARNING: gap {gap:.3f} mm is too narrow for a groove that cuts through the "
            f"copper without touching the neighbour at X{gx - origin_x:.2f} Y{gy - origin_y:.2f}: "
            "not isolated, copper stays connected"
        )
    lines.append("")

    lines.append("G21 ; mm mode")
    lines.append("G90 ; absolute positioning")
    lines.append(f"{settings.wcs} ; work coordinate system")
    lines.append(settings.z_move(settings.safe_z, "safe Z"))
    lines.append(f"M3 S{settings.spindle_speed} ; spindle on")
    lines.append(f"G4 P{settings.spindle_dwell:.1f} ; dwell for spindle spinup")
    lines.append("")

    total_paths = 0
    pos = (0.0, 0.0)

    def emit_ring(ring, depths, label):
        """Cut one closed ring; stay down between depth steps."""
        nonlocal total_paths
        coords = [(x - origin_x, y - origin_y) for x, y in ring]
        total_paths += 1
        lines.append(f"; Path {total_paths}{label}")
        lines.append(settings.z_move(settings.travel_z))
        lines.append(f"G0 X{coords[0][0]:.4f} Y{coords[0][1]:.4f}")
        for depth_idx, z_depth in enumerate(depths):
            if len(depths) > 1:
                lines.append(f"; Depth pass {depth_idx + 1}/{len(depths)} (Z={-z_depth:.4f})")
            lines.append(f"G1 Z{-z_depth:.4f} F{settings.plunge_rate:.0f}")
            lines.append(f"G1 X{coords[1][0]:.4f} Y{coords[1][1]:.4f} F{settings.feed_rate:.0f}")
            for x, y in coords[2:]:
                lines.append(f"G1 X{x:.4f} Y{y:.4f}")
        lines.append(settings.z_move(settings.travel_z))
        lines.append("")

    # Narrow-gap pre-pass: gaps between tool tip and trench width collapse
    # in the union buffer below. Each gap class is cut shallower, at an
    # offset where the V groove is exactly as wide as the narrowest gap of
    # the class (no undercut); see plan_narrow_gap_passes.
    for offset, depth, members, covers in narrow_classes:
        tight_paths, open_paths = _narrow_gap_paths(copper_geometry, members, offset)
        if not tight_paths and not open_paths:
            continue
        lines.append(
            f"; --- Narrow-gap pre-pass (offset {offset:.3f} mm, depth {depth:.3f} mm) ---"
        )
        lines.append(
            f"; Gaps {covers[0]:.3f} to {covers[1]:.3f} mm < effective width {ew:.3f} mm"
        )
        for ring in _order_rings(tight_paths, pos):
            emit_ring(ring, [depth], " (narrow-gap)")
            pos = ring[0]
        for path in _order_open_paths(open_paths, pos):
            emit_ring(path, [depth], " (narrow-gap)")
            pos = path[-1]

    num_passes = max_dynamic_passes if dynamic_mode else settings.passes
    actual_passes = 0
    for pass_num in range(num_passes):
        current_offset = ew / 2 + pass_num * step_over
        if dynamic_mode and current_offset > MAX_CLEAR_GAPS_OFFSET:
            break

        # Buffer the whole copper union at once so offset zones of nearby
        # features merge instead of producing duplicate cuts
        paths, n_polys, n_interiors = _buffer_copper_paths(copper_geometry, current_offset)
        if not paths:
            break

        actual_passes += 1
        lines.append(f"; --- Lateral Pass {pass_num + 1} (offset {current_offset:.3f} mm) ---")
        ordered = _order_rings(paths, pos)
        for ring in ordered:
            emit_ring(ring, depth_steps, "")
        pos = ordered[-1][0]

        if dynamic_mode and n_polys <= 1 and n_interiors == 0:
            break

    lines.append("; === End ===")
    lines.append(settings.z_move(settings.safe_z, "safe Z"))
    lines.append("M5 ; spindle off")
    lines.append("G0 X0 Y0 ; return to origin")
    lines.append("M2 ; end of program")

    _logger.info(
        f"Generated {total_paths} toolpaths ({actual_passes} lateral pass(es)), "
        f"{len(lines)} lines of G-code, {len(gaps)} unisolatable gap(s)"
    )
    return "\n".join(lines)


MIN_GROOVE_DEPTH = 0.035  # mm, deep enough to cut through ~35 um copper


def _polygons(geometry) -> list:
    """The polygons of a (Multi)Polygon as a list."""
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    return [g for g in getattr(geometry, "geoms", []) if isinstance(g, Polygon)]


def _narrow_gaps(copper, tool_diameter: float, effective_width: float) -> list[tuple]:
    """
    Gaps between neighbouring copper polygons that a V-bit can still
    isolate (tool_diameter <= gap < effective_width).

    Returns ``[(gap, i, j, (x, y)), ...]`` sorted by gap; i and j index the
    polygons of ``copper``.
    """
    from shapely.ops import nearest_points
    from shapely.strtree import STRtree

    polys = _polygons(copper)
    if len(polys) < 2:
        return []
    tree = STRtree(polys)
    found = []
    for i, poly in enumerate(polys):
        for j in tree.query(poly.buffer(effective_width)):
            if j <= i:
                continue
            dist = poly.distance(polys[j])
            if tool_diameter <= dist < effective_width:
                a, b = nearest_points(poly, polys[j])
                found.append((dist, i, int(j), ((a.x + b.x) / 2, (a.y + b.y) / 2)))
    return sorted(found)


def _find_min_processable_gap(
    copper: MultiPolygon, tool_diameter: float, effective_width: float
) -> float:
    """Narrowest gap a V-bit can still isolate (0 if there is none)."""
    gaps = _narrow_gaps(copper, tool_diameter, effective_width)
    return gaps[0][0] if gaps else 0.0


def _buffer_individual_paths(copper: MultiPolygon, offset: float) -> list[list[tuple]]:
    """Rings of every copper polygon buffered on its own (no union of the buffers)."""
    paths: list[list[tuple]] = []
    for poly in _polygons(copper):
        paths.extend(_rings(poly.buffer(offset, join_style=2, quad_segs=16)))
    return paths


def plan_narrow_gap_passes(copper, settings: ToolpathSettings) -> tuple[list, list]:
    """
    Plan the shallow pre-passes for gaps narrower than the trench width.

    Each gap is decided on its own, not from the single narrowest one. A
    groove centred ``o`` from a copper edge spans ``[0, 2 o]``; it stays out
    of the neighbour if the gap is at least ``2 o`` and the grooves of both
    edges meet if the gap is at most ``4 o``. So the narrowest open gap g
    starts a class with ``o = g / 2`` that serves all gaps up to ``2 g``;
    the next gap beyond that starts the next class.

    A gap whose own groove would be too shallow to cut through the copper
    (``o`` below the offset that gives MIN_GROOVE_DEPTH) cannot be cut
    without damaging the neighbour. It is reported in the second result.

    Returns ``(classes, uncut)``: classes are
    ``(offset, depth, polygon indices, (smallest gap, largest gap))``,
    uncut are ``(gap, (x, y))``.
    """
    ew = settings.effective_width
    tip = settings.tool_diameter
    tan_half = math.tan(math.radians(settings.tool_angle / 2))
    classes: list[list] = []
    uncut: list[tuple] = []
    for gap, i, j, location in _narrow_gaps(copper, tip, ew):
        member = next(
            (c for c in classes if 2 * c[0] <= gap + 1e-9 and gap <= 4 * c[0] + 1e-9), None
        )
        if member is None:
            offset = gap / 2
            depth = (offset - tip / 2) / tan_half if tan_half > 1e-9 else 0.0
            if depth < MIN_GROOVE_DEPTH:
                uncut.append((gap, location))
                continue
            member = [offset, depth, set(), (gap, gap)]
            classes.append(member)
        member[2].update((i, j))
        member[3] = (member[3][0], max(member[3][1], gap))
    return [tuple(c) for c in classes], uncut


def _narrow_gap_paths(copper, members: set, offset: float) -> tuple[list, list]:
    """
    Pre-pass paths around the polygons ``members``, ``offset`` from their edge.

    Every polygon is buffered on its own: merging the buffers would close the
    narrow slits. Parts of a path that come closer than ``offset`` to any
    copper would let the groove (half width ``offset``) cut into that copper;
    they are dropped. That happens next to still narrower gaps, which have
    their own pass, or sub-tip gaps, which cannot be cut at all.

    Returns ``(closed rings, open paths)``.
    """
    from shapely.ops import linemerge

    polys = _polygons(copper)
    zone = None
    closed: list[list[tuple]] = []
    opened: list[list[tuple]] = []
    for index in sorted(members):
        for ring in _rings(polys[index].buffer(offset, join_style=2, quad_segs=16)):
            if zone is None:
                zone = copper.buffer(offset - 1e-4)
            line = LineString(ring)
            if not line.intersects(zone):
                closed.append(ring)
                continue
            kept = line.difference(zone)
            pieces = list(kept.geoms) if hasattr(kept, "geoms") else [kept]
            pieces = [p for p in pieces if isinstance(p, LineString) and not p.is_empty]
            if len(pieces) > 1:
                merged = linemerge(pieces, directed=True)
                pieces = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
            for piece in pieces:
                if piece.length >= 0.05:
                    opened.append(list(piece.coords))
    return closed, opened


def _order_open_paths(paths: list[list[tuple]], start=(0.0, 0.0)) -> list[list[tuple]]:
    """Visit open paths nearest-start-first; the direction of each is kept."""
    remaining = list(paths)
    ordered = []
    pos = start
    while remaining:
        nearest = min(
            range(len(remaining)),
            key=lambda i: (remaining[i][0][0] - pos[0]) ** 2 + (remaining[i][0][1] - pos[1]) ** 2,
        )
        path = remaining.pop(nearest)
        ordered.append(path)
        pos = path[-1]
    return ordered


def _buffer_copper_paths(
    copper: MultiPolygon, offset: float
) -> tuple[list[list[tuple]], int, int]:
    """
    Generate isolation toolpaths by buffering the entire copper geometry.

    Buffering the whole MultiPolygon at once merges overlapping offset zones
    between nearby features (e.g. a pad inside a ground-plane clearance)
    into one path instead of duplicate cuts.

    Returns ``(rings, num_polygons, num_interiors)``; the topology counters
    let clear-gaps mode detect when all gaps are filled.
    """
    buffered = copper.buffer(offset, join_style=2, quad_segs=16)
    if buffered.is_empty:
        return [], 0, 0
    polys = [buffered] if isinstance(buffered, Polygon) else [
        g for g in getattr(buffered, "geoms", []) if isinstance(g, Polygon)
    ]
    num_interiors = sum(len(p.interiors) for p in polys)
    return _rings(buffered), len(polys), num_interiors


# =============================================================================
# Drilling G-code
# =============================================================================


def generate_drill_gcode(
    drills: list,
    settings: ToolpathSettings,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
    mirror: str | None = None,
    board_size: tuple = (0.0, 0.0),
) -> str:
    """
    Generate drilling G-code from drill info list.

    Tools run from the smallest to the largest diameter. Each tool starts
    with a tool change: tool up, spindle off, ``T<n> M6`` (the plugin pauses
    there and asks to change the drill and zero Z again), spindle on.
    ``settings.cut_depth`` holds the board thickness.
    """
    _logger.info(f"Generating drill G-code: {len(drills)} tools")
    depth = settings.cut_depth + settings.breakthrough_depth

    lines = []
    lines.append("; === Drilling G-code ===")
    if mirror:
        lines.append(f"; Back side: mirrored in {mirror} (board flipped)")
    lines.append(f"; Drill feed: {settings.drill_feed} mm/min")
    lines.append(
        f"; Drill depth: {depth:.2f} mm "
        f"(board {settings.cut_depth} mm + breakthrough {settings.breakthrough_depth} mm)"
    )
    lines.append(f"; Spindle speed: {settings.spindle_speed} RPM")
    lines.append("")

    lines.append("G21 ; mm mode")
    lines.append("G90 ; absolute positioning")
    lines.append(f"{settings.wcs} ; work coordinate system")
    lines.append(settings.z_move(settings.safe_z, "safe Z"))
    lines.append("")

    total_holes = 0
    pos = (0.0, 0.0)
    for drill in sorted((d for d in drills if d.holes), key=lambda d: d.diameter):
        lines.append(f"; --- Tool T{drill.tool_number}: {drill.diameter:.2f} mm ({len(drill.holes)} holes) ---")
        lines.append(settings.z_move(settings.safe_z, "tool change height"))
        lines.append("M5 ; spindle off for the tool change")
        lines.append(f"T{drill.tool_number} M6 ; drill {drill.diameter:.2f} mm")
        lines.append(f"M3 S{settings.spindle_speed} ; spindle on")
        lines.append(f"G4 P{settings.spindle_dwell:.1f} ; dwell for spindle spinup")
        lines.append("")

        holes = [
            (x - origin_x, y - origin_y) if not mirror
            else tuple(c - o for c, o in zip(
                mirror_point(x, y, mirror, (origin_x, origin_y), board_size), (origin_x, origin_y)))
            for x, y in drill.holes
        ]
        # Single holes in the shortest order; the overlapping holes of a
        # slot afterwards, in the order the parser gave them
        in_slot = drill.slot_indices() if hasattr(drill, "slot_indices") else set()
        single = [h for i, h in enumerate(holes) if i not in in_slot]
        sequence = _optimize_drill_order(single, pos)
        for first, length in getattr(drill, "slot_runs", []):
            sequence.append(f"; Slot: {length} overlapping holes")
            sequence.extend(holes[first:first + length])
        for hole in sequence:
            if isinstance(hole, str):
                lines.append(hole)
                continue
            hx, hy = hole
            total_holes += 1
            lines.append(f"G0 X{hx:.4f} Y{hy:.4f}")
            lines.append(settings.z_move(settings.retract_height))
            if depth <= settings.peck_depth or settings.peck_depth <= 0:
                lines.append(f"G1 Z{-depth:.4f} F{settings.drill_feed:.0f}")
            else:
                current_depth = 0.0
                while current_depth < depth - 1e-9:
                    current_depth = min(current_depth + settings.peck_depth, depth)
                    lines.append(f"G1 Z{-current_depth:.4f} F{settings.drill_feed:.0f}")
                    lines.append(settings.z_move(settings.retract_height, "retract"))
            lines.append(settings.z_move(settings.travel_z))
            pos = (hx, hy)
        lines.append("")

    lines.append("; === End ===")
    lines.append(settings.z_move(settings.safe_z, "safe Z"))
    lines.append("M5 ; spindle off")
    lines.append("G0 X0 Y0 ; return to origin")
    lines.append("M2 ; end of program")

    _logger.info(f"Generated drill G-code for {total_holes} holes")
    return "\n".join(lines)


def _optimize_drill_order(holes: list[tuple], start=None) -> list[tuple]:
    """
    Optimize drill order using nearest-neighbor heuristic, starting at the
    hole nearest to ``start`` (the current tool position) if given.
    Minimizes total travel distance between holes.
    """
    if len(holes) <= 1:
        return list(holes)

    remaining = list(holes)
    first = 0
    if start is not None:
        first = min(range(len(remaining)),
                    key=lambda i: (remaining[i][0] - start[0]) ** 2 + (remaining[i][1] - start[1]) ** 2)
    ordered = [remaining.pop(first)]

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


def board_shape_from_outline(outline_geometry) -> Polygon | None:
    """
    Board shape (with cutouts as holes) from the outline layer.

    Edge.Cuts lines are strokes: merged they form thin rings whose hole is
    the board. KiCad means the stroke centre line, so each closed stroke
    ring becomes the polygon of its centre line. The largest one is the
    board, rings inside it are cutouts. A filled polygon without holes (e.g.
    the bounding-box fallback) is taken as the board itself.
    """
    if isinstance(outline_geometry, (LineString, LinearRing)):
        try:
            outline_geometry = Polygon(outline_geometry.coords)
        except (ValueError, TypeError):
            return None
    polys = [outline_geometry] if isinstance(outline_geometry, Polygon) else [
        g for g in getattr(outline_geometry, "geoms", []) if isinstance(g, Polygon)
    ]
    regions = []
    for poly in polys:
        if poly.is_empty:
            continue
        if poly.interiors:
            # Stroke ring: width = area / mean of outer and inner length
            perimeter = poly.exterior.length + sum(r.length for r in poly.interiors)
            width = 2 * poly.area / perimeter if perimeter else 0.0
            region = Polygon(poly.exterior).buffer(-width / 2, join_style=2)
        else:
            region = poly
        if not region.is_empty:
            regions.append(region)
    if not regions:
        return None
    board = max(regions, key=lambda r: r.area)
    for region in regions:
        if region is not board and board.contains(region.representative_point()):
            board = board.difference(region)
    if isinstance(board, MultiPolygon):
        board = max(board.geoms, key=lambda p: p.area)
    return board


def _split_at_tabs(coords: list[tuple], tabs: list[tuple]) -> list[tuple]:
    """
    Split a closed path at the tab boundaries.

    Returns [(x, y, in_tab), ...]: the point reached and whether the
    stretch leading to it lies inside a tab.
    """
    def in_tab(dist):
        return any(start <= dist <= end for start, end in tabs)

    out = []
    walked = 0.0
    for (x0, y0), (x1, y1) in itertools.pairwise(coords):
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg == 0:
            continue
        cuts = sorted({b - walked for t in tabs for b in t if walked < b < walked + seg})
        prev = 0.0
        for c in cuts + [seg]:
            mid = walked + (prev + c) / 2
            f = c / seg
            out.append((x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, in_tab(mid)))
            prev = c
        walked += seg
    return out


def generate_outline_gcode(
    outline_geometry,
    settings: ToolpathSettings,
    board_thickness: float = 1.6,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
    mirror: str | None = None,
    board_size: tuple = (0.0, 0.0),
) -> str:
    """
    Generate outline/profile cutting G-code.

    Cutouts inside the board are milled first (tool inside them), then the
    outer contour (tool outside the board) with holding tabs. Every path is
    milled in steps of ``settings.outline_step_down`` down to board
    thickness plus breakthrough.
    """
    _logger.info("Generating outline G-code")
    board = board_shape_from_outline(outline_geometry)
    if board is None:
        _logger.error(f"Unsupported or empty outline geometry: {type(outline_geometry)}")
        return ""
    if mirror:
        board = mirror_geometry(board, mirror, (origin_x, origin_y), board_size)

    radius = settings.tool_diameter / 2
    # Outward offset moves the outer contour away from the board and shrinks
    # the cutouts, i.e. the tool stays outside the board everywhere
    tool_area = board.buffer(radius, join_style=2) if radius > 0 else board
    if isinstance(tool_area, MultiPolygon):
        tool_area = max(tool_area.geoms, key=lambda p: p.area)
    tool_area = orient(tool_area, sign=CLIMB_SIGN)
    outer = [(x - origin_x, y - origin_y) for x, y in tool_area.exterior.coords]
    cutouts = [[(x - origin_x, y - origin_y) for x, y in r.coords] for r in tool_area.interiors]

    total_depth = board_thickness + settings.breakthrough_depth
    step = settings.outline_step_down if settings.outline_step_down > 0 else total_depth
    depths = []
    d = 0.0
    while d < total_depth - 1e-9:
        d = min(d + step, total_depth)
        depths.append(d)
    tab_bottom = board_thickness - settings.tab_height  # tab top, measured from the surface

    lines = []
    lines.append("; === Outline Cutting G-code ===")
    if mirror:
        lines.append(f"; Back side: mirrored in {mirror} (board flipped)")
    lines.append(f"; Board thickness: {board_thickness} mm")
    lines.append(f"; Breakthrough: {settings.breakthrough_depth} mm (total depth {total_depth:.2f} mm)")
    lines.append(f"; Tool diameter: {settings.tool_diameter} mm, step down {step:.2f} mm ({len(depths)} passes)")
    lines.append(f"; Tabs: {settings.num_tabs} x {settings.tab_width} mm, {settings.tab_height} mm high")
    lines.append(f"; Cutouts: {len(cutouts)}")
    lines.append(CLIMB_NOTE)
    lines.append("")

    lines.append("G21 ; mm mode")
    lines.append("G90 ; absolute positioning")
    lines.append(f"{settings.wcs} ; work coordinate system")
    lines.append(settings.z_move(settings.safe_z, "safe Z"))
    lines.append(f"M3 S{settings.spindle_speed} ; spindle on")
    lines.append(f"G4 P{settings.spindle_dwell:.1f} ; dwell for spindle spinup")
    lines.append("")

    def cut_ring(coords, tabs, label):
        lines.append(f"; --- {label} ---")
        pieces = _split_at_tabs(coords, tabs)
        lines.append(settings.z_move(settings.travel_z))
        lines.append(f"G0 X{coords[0][0]:.4f} Y{coords[0][1]:.4f}")
        for depth in depths:
            lines.append(f"; Depth pass Z{-depth:.3f}")
            z = -depth
            lines.append(f"G1 Z{z:.4f} F{settings.plunge_rate:.0f}")
            feed_pending = True
            for x, y, tab in pieces:
                target = -min(depth, tab_bottom) if tab else -depth
                if abs(target - z) > 1e-9:
                    lines.append(f"G1 Z{target:.4f} F{settings.plunge_rate:.0f}")
                    z = target
                    feed_pending = True
                if feed_pending:
                    lines.append(f"G1 X{x:.4f} Y{y:.4f} F{settings.feed_rate:.0f}")
                    feed_pending = False
                else:
                    lines.append(f"G1 X{x:.4f} Y{y:.4f}")
        lines.append(settings.z_move(settings.travel_z))
        lines.append("")

    # Cutouts first, while the board is still held all around
    for n, ring in enumerate(cutouts, 1):
        cut_ring(ring, [], f"Cutout {n}")
    cut_ring(outer, _generate_tab_positions(outer, settings.num_tabs, settings.tab_width), "Outer contour")

    lines.append(settings.z_move(settings.safe_z, "safe Z"))
    lines.append("M5 ; spindle off")
    lines.append("G0 X0 Y0 ; return to origin")
    lines.append("M2 ; end of program")
    return "\n".join(lines)


def _generate_tab_positions(
    coords: list[tuple], num_tabs: int, tab_width: float
) -> list[tuple]:
    """
    Generate tab positions along the outline path.
    Returns list of (start_distance, end_distance) tuples.
    """
    total_length = sum(
        math.hypot(x1 - x0, y1 - y0) for (x0, y0), (x1, y1) in itertools.pairwise(coords)
    )
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
    mirror: str | None = None,
    board_size: tuple = (0.0, 0.0),
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
    if mirror:
        board_poly = mirror_geometry(board_poly, mirror, (origin_x, origin_y), board_size)
        if mask_geometry is not None and not mask_geometry.is_empty:
            mask_geometry = mirror_geometry(mask_geometry, mirror, (origin_x, origin_y), board_size)
        bx0, by0, bx1, by1 = board_poly.bounds

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
    # Neighbouring scan lines overlap: the spot is brighter in the middle,
    # lines exactly one spot apart would leave under-cured stripes
    line_spacing = spot * (1.0 - min(max(settings.uv_line_overlap, 0.0), 90.0) / 100.0)

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
    lines.append(f"; UV spot size: {spot} mm, line spacing {line_spacing:.3f} mm")
    if mirror:
        lines.append(f"; Back side: mirrored in {mirror} (board flipped)")
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
    scan_y_start = ka[1]  # keep_area is already eroded by spot / 2
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

        y += line_spacing
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


# =============================================================================
# Alignment compensation for double-sided boards
# =============================================================================


def back_side_alignment_holes(
    holes: list[tuple[float, float, float]],
    board_min: tuple[float, float],
    board_size: tuple[float, float],
    flip_axis: str = "X",
) -> list[dict]:
    """
    Pick the two holes farthest apart and give their positions on the back side.

    ``holes`` are (x, y, diameter) in drill-file coordinates. The drill
    G-code puts them at (x - min_x, y - min_y); after flipping the board
    (``flip_axis`` "X" mirrors X, "Y" mirrors Y, as mirror_gcode does) they
    are expected at the mirrored work coordinates returned here.
    """
    if len(holes) < 2:
        raise ValueError("need at least 2 holes")
    best = (0.0, 0, 1)
    for i in range(len(holes)):
        for j in range(i + 1, len(holes)):
            dist = (holes[i][0] - holes[j][0]) ** 2 + (holes[i][1] - holes[j][1]) ** 2
            if dist > best[0]:
                best = (dist, i, j)
    width, height = board_size
    result = []
    for k in best[1:]:
        x, y, diameter = holes[k]
        hx, hy = x - board_min[0], y - board_min[1]
        if flip_axis == "X":
            hx = width - hx
        else:
            hy = height - hy
        result.append({"expected_x": round(hx, 4), "expected_y": round(hy, 4), "diameter": diameter})
    return result


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

    # The translation of the scaled solution does not belong to the pure
    # rotation: with a scale error it would be off by (scale - 1) * R * p.
    # For a fixed rotation the least-squares translation maps centroid to
    # centroid.
    cx = sum(p[0] for p in expected) / n
    cy = sum(p[1] for p in expected) / n
    mcx = sum(p[0] for p in measured) / n
    mcy = sum(p[1] for p in measured) / n
    tx = mcx - (a * cx - b * cy)
    ty = mcy - (b * cx + a * cy)

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


_WORD_X = re.compile(r"X([+-]?(?:\d+\.?\d*|\.\d+))", re.IGNORECASE)
_WORD_Y = re.compile(r"Y([+-]?(?:\d+\.?\d*|\.\d+))", re.IGNORECASE)
_MOTION_WORD = re.compile(r"G0*([0123])(?![\d.])", re.IGNORECASE)


def apply_alignment_transform(gcode: str, transform: AlignmentTransform) -> str:
    """Apply a rigid-body alignment transform to all XY coordinates in G-code.

    Rewrites the X and Y values of G0/G1 moves:
        x' = cos_θ · x − sin_θ · y + tx
        y' = sin_θ · x + cos_θ · y + ty

    A rotation couples the axes: a move that names only X also changes Y.
    The position is therefore tracked, and every transformed move gets
    both words. Comments are left alone. Arcs (G2/G3) and relative moves
    (G91) cannot be transformed this way and raise ValueError instead of
    producing a wrong path.
    """
    a = transform.cos_theta
    b = transform.sin_theta
    tx = transform.tx
    ty = transform.ty

    # If the transform is essentially identity, skip the work
    if abs(b) < 1e-7 and abs(tx) < 1e-5 and abs(ty) < 1e-5 and abs(a - 1.0) < 1e-7:
        return gcode

    cur_x = cur_y = 0.0
    output = []
    for line in gcode.splitlines():
        code, sep, comment = line.partition(";")
        if not code.strip() or code.lstrip().startswith("("):
            output.append(line)
            continue
        upper = code.upper()
        if re.search(r"G91(?![\d.])", upper):
            raise ValueError("relative moves (G91) cannot be aligned")

        x_match = _WORD_X.search(code)
        y_match = _WORD_Y.search(code)
        if not x_match and not y_match:
            output.append(line)
            continue
        motion = _MOTION_WORD.search(code)
        if motion and motion.group(1) in "23":
            raise ValueError("arcs (G2/G3) cannot be aligned")
        if re.search(r"G(?:10|28|30|53|92)(?![\d])", upper):
            # Offsets and machine coordinates are not positions on the board
            output.append(line)
            continue

        if x_match:
            cur_x = float(x_match.group(1))
        if y_match:
            cur_y = float(y_match.group(1))
        new_x = a * cur_x - b * cur_y + tx
        new_y = b * cur_x + a * cur_y + ty

        if x_match and y_match:
            code = _WORD_X.sub(f"X{new_x:.4f}", code, count=1)
            code = _WORD_Y.sub(f"Y{new_y:.4f}", code, count=1)
        elif x_match:
            code = _WORD_X.sub(f"X{new_x:.4f} Y{new_y:.4f}", code, count=1)
        else:
            code = _WORD_Y.sub(f"X{new_x:.4f} Y{new_y:.4f}", code, count=1)
        output.append(code + sep + comment)

    return "\n".join(output)


# =============================================================================
# Pre-flight: does the job fit the machine?
# =============================================================================

_WORD_Z = re.compile(r"Z([+-]?(?:\d+\.?\d*|\.\d+))", re.IGNORECASE)


def gcode_extent(gcode: str) -> dict | None:
    """
    Smallest and largest X, Y and Z a G-code program moves to (work
    coordinates), as {"x": (min, max), ...}; None if it does not move.
    Axes the program never names are left out.
    """
    extent: dict = {}
    for line in gcode.splitlines():
        code = line.partition(";")[0]
        if not code.strip() or code.lstrip().startswith("("):
            continue
        if not _MOTION_WORD.search(code) or re.search(r"G(?:10|28|30|53|92)(?![\d])", code, re.IGNORECASE):
            continue
        for axis, pattern in (("x", _WORD_X), ("y", _WORD_Y), ("z", _WORD_Z)):
            match = pattern.search(code)
            if match:
                value = float(match.group(1))
                low, high = extent.get(axis, (value, value))
                extent[axis] = (min(low, value), max(high, value))
    return extent or None


def travel_check(extent: dict, limits: dict, work_offset: dict, machine_pos: dict) -> list[dict]:
    """
    Compare the extent of a job with the travel of the machine.

    ``limits`` comes from ConnectedGrblPrinter.travel_limits(). Two checks:

    - "job_larger_than_travel": the job spans more than the axis can travel
      at all. Independent of homing.
    - "job_outside_travel": with homing enabled the machine coordinates are
      known, so the job can be placed: GRBL's travel runs from the homed end
      in one direction, 0 to -travel by default, 0 to +travel on builds that
      set the origin at the other end. Which one applies is read from the
      sign of the current machine position; if the machine stands exactly
      at its origin the placement cannot be judged and is skipped.

    Returns a list of warning dicts with ``code``, ``axis`` and ``by`` (mm).
    """
    tolerance = 0.01
    warnings = []
    for axis in ("x", "y", "z"):
        if axis not in extent:
            continue
        low, high = extent[axis]
        travel = limits["travel"].get(axis, 0.0)
        if travel <= 0:
            continue
        if high - low > travel + tolerance:
            warnings.append({
                "code": "job_larger_than_travel", "axis": axis.upper(),
                "by": round(high - low - travel, 2),
            })
            continue
        if not limits.get("homing"):
            continue
        offset = work_offset.get(axis, 0.0)
        m_low, m_high = low + offset, high + offset
        position = machine_pos.get(axis, 0.0)
        if position < -tolerance:
            allowed = (-travel, 0.0)
        elif position > tolerance:
            allowed = (0.0, travel)
        else:
            continue
        over = max(allowed[0] - m_low, m_high - allowed[1])
        if over > tolerance:
            warnings.append({"code": "job_outside_travel", "axis": axis.upper(), "by": round(over, 2)})
    return warnings
