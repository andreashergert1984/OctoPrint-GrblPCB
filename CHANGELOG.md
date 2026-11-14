# Changelog

All notable changes to OctoPrint-GrblPCB will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — Unreleased

### Changed

- A job now ends as completed, stopped or failed. A GRBL error stops the
  job and halts the machine instead of sending the remaining lines (the
  rejected line may have been the retract before a travel move). An alarm
  or a lost connection is reported as failed, no longer as completed
- "Completed" and the tool change prompt wait until GRBL reports Idle; `ok`
  only means a line reached the planner
- Stopping a job waits for the feed hold to complete before the soft
  reset, so the position is kept (no ALARM:3)
- API commands check OctoPrint permissions: Control for everything that
  moves the machine, file permissions for project changes, Status for reads
- Connecting fails with a clear message if no GRBL status report arrives
  (wrong port or baud rate) and closes the port again

- Pre-flight check against the machine travel (GRBL `$130` to `$132`,
  read on connect): a job larger than the machine, or with homing enabled
  a job that leaves the travel with the current work zero, raises a
  warning before the start
- Manual G-code from OctoPrint's terminal or control tab is no longer mixed
  into a running job; it is refused with a line in the terminal log
- Excellon `G85` slots are drilled as rows of overlapping holes (every
  second hole first); routed paths (`M15`) still only raise a warning
- Wide view: a button lets the workspace use the whole page width
- Confirmations use OctoPrint's dialog; setting the X/Y zero asks first
  when a height map exists; machine buttons are disabled while not
  connected; the Autolevel tab explains the preparation
- User interface: the workspace fits the board into view by itself after
  an upload or when a project loads (it stayed empty until "Fit view" was
  clicked); labels, the current workflow step and selected buttons are
  readable (dark grey on colour before); jog keys are larger; a hint tells
  how to connect while not connected; the empty operations list links to
  the Board tab

### Fixed

- The file input was never cleared (wrong element id), so choosing the
  same files again did not upload them
- A lost serial connection (cable pulled) is detected: the job fails, the
  connection goes to "Offline after error" and the user is told
- Autolevel and alignment probing stop when GRBL rejects a positioning
  move, instead of probing (or plunging) at the previous position
- Clear polarity (`%LPC*%`) in Gerber files erases earlier copper instead
  of being added as copper
- Alignment transform also rotates moves that name only X or only Y,
  leaves comments alone and refuses arcs and relative moves
- `project.json` is written atomically; a crash while saving no longer
  destroys the project
- Final job message carries the real line number and 100 %
- Path ordering for isolation milling is about 40 times faster on boards
  with many contours (1200 contours: 10 s to 0.25 s)
- Operation indices from the API are validated (negative or non-integer
  values were accepted)
- Drill files with routed slots raise a warning instead of silently
  drilling a single hole
- Streaming was throttled to about 15 lines/s because GRBL responses were
  only read by the 4 Hz status poll; a reader thread now handles every
  response as it arrives (about 370 lines/s against the simulator)
- Status reports with additional fields (`Bf`, `Ln`, `Pn`) lost the work
  offset and overrides; fields are now parsed in any order

### Added

- GRBL simulator (`tools/grbl_simulator.py`) for testing without hardware
- GRBL port list honours OctoPrint's additional serial ports setting

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

[0.1.0]: https://git.hergert.info/octoprint/OctoPrint-GrblPCB/-/tags/v0.1.0

