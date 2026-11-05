# Changelog

All notable changes to OctoPrint-GrblPCB will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — Unreleased

### Added

- **Gerber & Excellon Import** — parse Gerber RS-274X and Excellon drill files
  via pygerber + Shapely; automatic unit / format detection; SVG preview
- **Isolation Routing** — V-bit engraving with configurable tool diameter,
  angle, cut depth, and multi-pass support
- **Drill Toolpaths** — optimised drill ordering with peck-drilling
- **Outline Cutting** — board profile routing with breakout tabs
- **Autolevel (Height-Map Probing)** — rectangular probe grid, G38.2 touch
  cycles, bilinear/cubic Z-correction applied per G-code line
- **Double-Sided PCB Alignment** — fiducial-hole probing, least-squares rigid
  transform, automatic coordinate correction for the back side
- **3-D Workspace Renderer** — real-time Canvas-based board visualisation with
  orbit / pan / zoom; colour-coded rapid vs. cutting moves
- **GRBL 1.1 Connector** — native OctoPrint connection type; homing, unlock,
  feed hold, resume, soft reset; real-time status polling (MPos/WPos, feed,
  spindle); character-counting streaming
- **Work Coordinate System Management** — G54–G59 selection, per-axis zeroing,
  goto-zero
- **Real-Time Overrides** — feed, rapid, and spindle override via GRBL
  single-byte commands
- **Tool-Change & Pause Handling** — M6 / M0 with user confirmation dialog
- **Spindle Control** — on/off with configurable RPM, startup delay, min/max
  limits
- **Project Management** — per-project directory with source files +
  `project.json`; save / load / delete; auto-restore on startup
- **Board Rotation** — arbitrary angle, always recomputed from original
  geometry (no cumulative drift)
- **Operation Reordering** — drag-and-drop operation list with delete and
  G-code download per operation
- **Toolpath Preview** — instant per-operation G-code preview in the 3-D
  viewer
- **Settings Panel** — configurable feed rates, probe depth, tool parameters,
  spindle defaults, drill peck settings, outline tab dimensions

### Security

- All API endpoints require OctoPrint `USER` permission
- File uploads validated: extension whitelist, MIME-type check, 50 MB limit
- Filename sanitisation via `octoprint.util.get_dos_short_path_name` /
  `secure_filename`
- Path-traversal protection on all file operations

### Code Quality

- Clean dispatch-table API router (`_CNC_COMMANDS` / `_PROJECT_COMMANDS`
  frozensets) with individual `_handle_*` methods — no monolithic if/elif
  chains
- Thread-safe project state behind `threading.Lock`
- All inline CSS extracted to semantic class names in `grblpcb.css`
- Input validation on every API command (axes, WCS, numeric ranges, enums)
- Proper HTTP status codes (400, 404, 409, 500) with JSON error bodies
- Structured logging via OctoPrint's `self._logger`

[0.1.0]: https://github.com/ahergert/OctoPrint-GrblPCB/releases/tag/v0.1.0

