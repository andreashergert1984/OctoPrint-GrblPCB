# -*- coding: utf-8 -*-
"""
PCB Project Model

Manages a collection of Gerber and drill files that make up a PCB project.
Tracks board dimensions, layers, and processing state.
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


_logger = logging.getLogger(__name__)


class LayerType(str, Enum):
    """Types of PCB layers."""
    TOP_COPPER = "top_copper"
    BOTTOM_COPPER = "bottom_copper"
    TOP_SILK = "top_silk"
    BOTTOM_SILK = "bottom_silk"
    TOP_MASK = "top_mask"
    BOTTOM_MASK = "bottom_mask"
    OUTLINE = "outline"
    DRILL = "drill"


# Common file extension to layer type mapping
EXTENSION_MAP = {
    # KiCad extensions
    ".gtl": LayerType.TOP_COPPER,
    ".gbl": LayerType.BOTTOM_COPPER,
    ".gto": LayerType.TOP_SILK,
    ".gbo": LayerType.BOTTOM_SILK,
    ".gts": LayerType.TOP_MASK,
    ".gbs": LayerType.BOTTOM_MASK,
    ".gm1": LayerType.OUTLINE,
    ".gko": LayerType.OUTLINE,
    # KiCad 6+ extensions
    "-f_cu.gbr": LayerType.TOP_COPPER,
    "-b_cu.gbr": LayerType.BOTTOM_COPPER,
    "-f_silkscreen.gbr": LayerType.TOP_SILK,
    "-b_silkscreen.gbr": LayerType.BOTTOM_SILK,
    "-f_mask.gbr": LayerType.TOP_MASK,
    "-b_mask.gbr": LayerType.BOTTOM_MASK,
    "-edge_cuts.gbr": LayerType.OUTLINE,
    # Drill files
    ".drl": LayerType.DRILL,
    ".exc": LayerType.DRILL,
    ".xln": LayerType.DRILL,
}


class BoardSide(str, Enum):
    """Which side of the board."""
    SINGLE = "single"
    DOUBLE = "double"


class ProjectState(str, Enum):
    """Processing state of a project."""
    IMPORTED = "imported"
    PARSED = "parsed"
    TOOLPATHS_GENERATED = "toolpaths_generated"
    PROBED = "probed"
    LEVELED = "leveled"
    READY = "ready"


@dataclass
class PCBLayer:
    """A single layer in the PCB project."""
    layer_type: LayerType
    filename: str
    filepath: str
    # Parsed geometry info
    min_x: float = 0.0
    min_y: float = 0.0
    max_x: float = 0.0
    max_y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    parsed: bool = False
    # SVG preview (base64 or path)
    svg_preview: str = ""

    def to_dict(self) -> dict:
        return {
            "layer_type": self.layer_type.value,
            "filename": self.filename,
            "filepath": self.filepath,
            "min_x": self.min_x,
            "min_y": self.min_y,
            "max_x": self.max_x,
            "max_y": self.max_y,
            "width": self.width,
            "height": self.height,
            "parsed": self.parsed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PCBLayer":
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields and k != "svg_preview"}
        filtered["layer_type"] = LayerType(data["layer_type"])
        return cls(**filtered)


@dataclass
class DrillInfo:
    """Information about drill holes."""
    tool_number: int
    diameter: float  # mm
    holes: list  # list of (x, y) tuples
    count: int = 0

    def to_dict(self) -> dict:
        return {
            "tool_number": self.tool_number,
            "diameter": self.diameter,
            "holes": self.holes,
            "count": self.count,
        }


@dataclass
class MillingOperation:
    """A single milling operation to perform."""
    name: str
    operation_type: str  # "isolation", "drill", "outline"
    layer_type: str
    gcode: str = ""
    # Derived versions, each built from the one before:
    # gcode -> gcode_aligned (double-sided alignment) -> gcode_leveled
    gcode_aligned: str = ""
    gcode_leveled: str = ""  # G-code with autolevel correction
    mirrored: bool = False  # bottom-side copy made by mirror_gcode
    tool_diameter: float = 0.1  # mm
    tool_angle: float = 60.0  # degrees (for V-bits)
    cut_depth: float = -0.1  # mm
    feed_rate: float = 500.0  # mm/min
    spindle_speed: int = 10000  # RPM
    passes: int = 1
    pass_overlap: float = 0.0  # mm extra offset per pass
    estimated_time: float = 0.0  # seconds
    completed: bool = False

    @property
    def gcode_to_send(self) -> str:
        """The most processed version: leveled, else aligned, else raw."""
        return self.gcode_leveled or self.gcode_aligned or self.gcode

    @property
    def gcode_to_level(self) -> str:
        """Autolevel works on the aligned version if there is one."""
        return self.gcode_aligned or self.gcode

    def to_dict(self, include_gcode: bool = False) -> dict:
        d = {
            "name": self.name,
            "operation_type": self.operation_type,
            "layer_type": self.layer_type,
            "tool_diameter": self.tool_diameter,
            "tool_angle": self.tool_angle,
            "cut_depth": self.cut_depth,
            "feed_rate": self.feed_rate,
            "spindle_speed": self.spindle_speed,
            "passes": self.passes,
            "pass_overlap": self.pass_overlap,
            "estimated_time": self.estimated_time,
            "completed": self.completed,
            "has_gcode": bool(self.gcode),
            "has_aligned_gcode": bool(self.gcode_aligned),
            "has_leveled_gcode": bool(self.gcode_leveled),
            "mirrored": self.mirrored,
            "gcode_lines": self.gcode.count("\n") + 1 if self.gcode else 0,
        }
        if include_gcode:
            d["gcode"] = self.gcode
            d["gcode_aligned"] = self.gcode_aligned
            d["gcode_leveled"] = self.gcode_leveled
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "MillingOperation":
        return cls(
            name=data["name"],
            operation_type=data["operation_type"],
            layer_type=data["layer_type"],
            gcode=data.get("gcode", ""),
            gcode_aligned=data.get("gcode_aligned", ""),
            gcode_leveled=data.get("gcode_leveled", ""),
            # Older projects only marked mirrored copies by name
            mirrored=data.get("mirrored", "(mirrored)" in data.get("name", "")),
            tool_diameter=data.get("tool_diameter", 0.1),
            tool_angle=data.get("tool_angle", 60.0),
            cut_depth=data.get("cut_depth", -0.1),
            feed_rate=data.get("feed_rate", 500.0),
            spindle_speed=data.get("spindle_speed", 10000),
            passes=data.get("passes", 1),
            pass_overlap=data.get("pass_overlap", 0.0),
            estimated_time=data.get("estimated_time", 0.0),
            completed=data.get("completed", False),
        )


@dataclass
class PCBProject:
    """A complete PCB project with all layers and operations."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = "Untitled PCB"
    layers: dict = field(default_factory=dict)  # LayerType -> PCBLayer
    drills: list = field(default_factory=list)  # list of DrillInfo
    operations: list = field(default_factory=list)  # list of MillingOperation
    state: ProjectState = ProjectState.IMPORTED
    board_side: BoardSide = BoardSide.SINGLE

    # Board dimensions (from parsed layers)
    board_width: float = 0.0
    board_height: float = 0.0
    board_min_x: float = 0.0
    board_min_y: float = 0.0

    # Double-sided settings
    flip_axis: str = "X"  # mirror axis for double-sided
    alignment_holes: list = field(default_factory=list)  # [{expected_x, expected_y, diameter, measured_x, measured_y, probed}]
    alignment_transform: Optional[dict] = None  # computed rigid-body transform
    # When the alignment holes were probed: the board was flipped before
    alignment_probed_at: Optional[float] = None

    # Accumulated rotation (degrees CCW) applied to gerber data
    rotation: float = 0.0

    # Autolevel data
    height_map: Optional[dict] = None  # stored probe data

    def add_layer(self, layer: PCBLayer):
        """Add or replace a layer."""
        self.layers[layer.layer_type] = layer
        self._update_board_dimensions()

    def _update_board_dimensions(self):
        """Update board dimensions from parsed layers."""
        # Reset to initial values so recalculation works correctly
        self.board_width = 0.0
        self.board_height = 0.0
        self.board_min_x = float('inf')
        self.board_min_y = float('inf')

        outline = self.layers.get(LayerType.OUTLINE)
        if outline and outline.parsed:
            self.board_width = outline.width
            self.board_height = outline.height
            self.board_min_x = outline.min_x
            self.board_min_y = outline.min_y
            return

        # Fall back to copper layer extents
        for lt in (LayerType.TOP_COPPER, LayerType.BOTTOM_COPPER):
            layer = self.layers.get(lt)
            if layer and layer.parsed:
                self.board_width = max(self.board_width, layer.width)
                self.board_height = max(self.board_height, layer.height)
                self.board_min_x = min(self.board_min_x, layer.min_x)
                self.board_min_y = min(self.board_min_y, layer.min_y)

        # Ensure board_min values are finite (no layers case)
        if self.board_min_x == float('inf'):
            self.board_min_x = 0.0
        if self.board_min_y == float('inf'):
            self.board_min_y = 0.0

    def has_bottom_layer(self) -> bool:
        return LayerType.BOTTOM_COPPER in self.layers

    def to_dict(self, include_gcode: bool = False) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "layers": {
                lt.value if isinstance(lt, LayerType) else lt: layer.to_dict()
                for lt, layer in self.layers.items()
            },
            "drills": [d.to_dict() for d in self.drills],
            "operations": [op.to_dict(include_gcode=include_gcode) for op in self.operations],
            "state": self.state.value,
            "board_side": self.board_side.value,
            "board_width": self.board_width,
            "board_height": self.board_height,
            "board_min_x": self.board_min_x,
            "board_min_y": self.board_min_y,
            "flip_axis": self.flip_axis,
            "alignment_holes": self.alignment_holes,
            "alignment_transform": self.alignment_transform,
            "alignment_probed_at": self.alignment_probed_at,
            "rotation": self.rotation,
            "has_height_map": self.height_map is not None,
            "height_map": self.height_map,
        }

    def get_project_dir(self, base_dir: str) -> str:
        """Return the per-project subdirectory path."""
        return os.path.join(base_dir, self.id)

    def save(self, base_dir: str):
        """Save project to its own subdirectory under base_dir.

        Directory structure:
            base_dir/<project_id>/
                project.json   - project metadata, layers, drills, operations (with gcode)
                <gerber files>  - uploaded source files
        """
        project_dir = self.get_project_dir(base_dir)
        os.makedirs(project_dir, exist_ok=True)
        filepath = os.path.join(project_dir, "project.json")
        with open(filepath, "w") as f:
            json.dump(self.to_dict(include_gcode=True), f, indent=2)
        _logger.info(f"Project saved: {filepath}")

    @classmethod
    def load(cls, project_dir: str, include_gcode: bool = True) -> "PCBProject":
        """Load project from a project directory containing project.json.

        Args:
            project_dir: Path to directory containing project.json.
            include_gcode: If False, skip loading G-code strings from
                operations to reduce memory usage (useful for listings).
        """
        filepath = os.path.join(project_dir, "project.json")
        with open(filepath) as f:
            data = json.load(f)

        project = cls(
            id=data["id"],
            name=data["name"],
            state=ProjectState(data["state"]),
            board_side=BoardSide(data["board_side"]),
            board_width=data.get("board_width", 0),
            board_height=data.get("board_height", 0),
            board_min_x=data.get("board_min_x", 0),
            board_min_y=data.get("board_min_y", 0),
            flip_axis=data.get("flip_axis", "X"),
            alignment_holes=data.get("alignment_holes", []),
            alignment_transform=data.get("alignment_transform"),
            alignment_probed_at=data.get("alignment_probed_at"),
            rotation=data.get("rotation", 0.0),
            height_map=data.get("height_map"),
        )

        # Restore layers
        for lt_str, layer_data in data.get("layers", {}).items():
            layer = PCBLayer.from_dict(layer_data)
            project.layers[layer.layer_type] = layer

        # Restore drills
        for drill_data in data.get("drills", []):
            project.drills.append(DrillInfo(
                tool_number=drill_data["tool_number"],
                diameter=drill_data["diameter"],
                holes=drill_data["holes"],
                count=drill_data.get("count", len(drill_data["holes"])),
            ))

        # Restore operations
        for op_data in data.get("operations", []):
            if include_gcode:
                project.operations.append(MillingOperation.from_dict(op_data))
            else:
                # Lightweight: create operation without G-code strings
                project.operations.append(MillingOperation(
                    name=op_data["name"],
                    operation_type=op_data["operation_type"],
                    layer_type=op_data["layer_type"],
                    gcode="",
                    gcode_leveled="",
                    tool_diameter=op_data.get("tool_diameter", 0.1),
                    tool_angle=op_data.get("tool_angle", 60.0),
                    cut_depth=op_data.get("cut_depth", -0.1),
                    feed_rate=op_data.get("feed_rate", 500.0),
                    spindle_speed=op_data.get("spindle_speed", 10000),
                    passes=op_data.get("passes", 1),
                    pass_overlap=op_data.get("pass_overlap", 0.0),
                    completed=op_data.get("completed", False),
                ))

        # Recalculate board dimensions from actual layer data
        # (stored values may be stale if computation was fixed)
        project._update_board_dimensions()

        _logger.info(
            f"Project loaded: {project.name} ({project.id}), "
            f"{len(project.layers)} layers, {len(project.operations)} operations"
        )
        return project

    def summary(self, project_dir: str = None) -> dict:
        """Return a lightweight summary for project listings (no gcode)."""
        layer_names = [
            lt.value if isinstance(lt, LayerType) else lt
            for lt in self.layers.keys()
        ]
        created = None
        if project_dir:
            project_json = os.path.join(project_dir, "project.json")
            if os.path.isfile(project_json):
                created = os.path.getmtime(project_json)
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state.value,
            "board_width": self.board_width,
            "board_height": self.board_height,
            "layers": layer_names,
            "operations_count": len(self.operations),
            "has_height_map": self.height_map is not None,
            "created": created,
        }


def guess_layer_type(filename: str) -> Optional[LayerType]:
    """Guess the layer type from filename/extension."""
    name_lower = filename.lower()

    # Check compound extensions first (e.g., "-F_Cu.gbr")
    for suffix, layer_type in EXTENSION_MAP.items():
        if name_lower.endswith(suffix):
            return layer_type

    # Check by extension alone
    ext = os.path.splitext(name_lower)[1]
    return EXTENSION_MAP.get(ext)


def list_projects(base_dir: str) -> list:
    """List all saved projects in base_dir.

    Returns a list of project summaries (lightweight, no gcode).
    """
    projects = []
    if not os.path.isdir(base_dir):
        return projects

    for entry in sorted(os.listdir(base_dir)):
        project_dir = os.path.join(base_dir, entry)
        project_json = os.path.join(project_dir, "project.json")
        if os.path.isdir(project_dir) and os.path.isfile(project_json):
            try:
                project = PCBProject.load(project_dir, include_gcode=False)
                projects.append(project.summary(project_dir=project_dir))
            except Exception as e:
                _logger.warning(f"Could not load project from {project_dir}: {e}")
    return projects


def delete_project(base_dir: str, project_id: str) -> bool:
    """Delete a project directory by id. Returns True if deleted."""
    import shutil

    project_dir = os.path.realpath(os.path.join(base_dir, project_id))
    if not project_dir.startswith(os.path.realpath(base_dir) + os.sep):
        _logger.warning(f"Path traversal blocked in delete_project: {project_id}")
        return False
    if os.path.isdir(project_dir):
        shutil.rmtree(project_dir)
        _logger.info(f"Deleted project: {project_id}")
        return True
    return False
