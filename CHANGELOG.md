# Changelog

All notable changes to OctoPrint-GrblPCB will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — Unreleased

### Fixed (second review of 2026-11-20)

- After a tool change or program pause the tool goes to safe Z before the
  spindle starts and before any XY move (it moved sideways at the probed
  height); the drill program lifts before `M3`
- Probing never uses relative mode: absolute targets from a fresh position,
  `G90` after homing, every probe run starts with `G21 G90` and its WCS;
  a refused or failed probe ends at once and is reported
- "Go to 0,0" stops at safe Z; alignment probing lowers into a hole with a
  probe move and reports a hole the bit cannot enter
- Stop or Reset during the Z drift check ends it at once; a silent GRBL is
  halted and treated as a lost connection; no new job starts while a stop
  is still being carried out; the Z zero is dropped when an alarm loses the
  position
- A page loaded during a job shows Stop and Confirm again
- Rotating back to 0° restores the board size; rotation and a changed drill
  layer reset the alignment; autolevel no longer adds a move to X0 Y0
- Gerber polygon apertures and thermals, Excellon tool selection with feed
  and speed; isolation passes use round corners
- The "Check" button works; the back side is drawn aligned with the copper;
  actions that discard leveled or aligned G-code ask first; consistent
  German terms

### Fixed (review of 2026-11-20)

- Machine safety: a soft reset or OctoPrint's Cancel during a job ends the
  job (the stream used to continue after GRBL had reset); after a tool
  change or program pause the job restores `G21 G90`, the work coordinate
  system, feed and spindle before it continues; the job runs in the work
  coordinate system that is active and zeroed (it used to switch back to
  the generated `G54`); jog, zero, probe and project changes are refused
  while autolevel or alignment probing runs; stopping probing halts the
  machine; alignment probing always lifts the tool out of the hole; an
  unexpected error halts the machine; a job only starts when GRBL is Idle;
  the RX buffer counts bytes, not characters
- OctoPrint integration: OctoPrint's Pause/Resume/Cancel act on the job;
  the connector no longer claims the unimplemented `job_on_hold`
- Isolation: narrow gaps are planned one by one (one very narrow gap used to
  leave all other narrow gaps uncut); the narrow-gap pass keeps clear of
  neighbouring copper; gaps that stay uncut are reported with their position
- Milling direction: real climb milling for a clockwise spindle (`M3`);
  the code milled conventionally while saying climb
- Gerber: step-and-repeat and block apertures, regions with several
  contours, macro primitives with exposure off, apertures with holes and
  lines drawn with rectangular apertures are imported correctly
- Excellon: route mode no longer creates holes, incremental coordinates and
  tool definitions in the body are read
- The work origin is the centre line of the outline; without an outline the
  board is the union of both copper layers
- Double-sided boards: bottom copper and bottom mask are always generated
  mirrored; old unmirrored bottom operations are marked outdated and cannot
  be sent; only drilling and the outline offer "mirror for the back side";
  the alignment fit computes the offset for the final rotation
- Autolevel: arcs update the tracked position and are split along the
  height map; the mask exposure covers the bottom edge
- Removing a layer asks first and removes the operations made from it (all
  operations when the board extent changes); removing the drill layer
  removes the holes
- Probing: "Probe Z and zero" probes, sets Z = 0 at the contact and lifts
  the tool; a manual Z probe reaches at least 5 mm below the safe height and
  the current zero (5 mm from the safe height did not reach the board after
  a tool change); "Go to zero" moves in absolute millimetres
- User interface: drill holes are drawn; the layer checkboxes work and hide
  their layer; layers are listed in a fixed order; X/Y/Z column headers;
  failed commands are shown instead of only logged; job messages are
  coloured by severity; Send is disabled with the reason in its tooltip;
  "Confirm tool change" only shows in a pause; homing is possible from
  alarm; a new probe grid asks before discarding measured heights; probing
  asks first when Z is not zeroed; alignment holes show their position;
  generation settings no longer scroll inside the panel; the view stays
  fitted when the panel changes size; in wide view a button shows the
  connection panel
- Development: compatible with OctoPrint 2.0.x and the current `dev`
  branch; ruff settings pinned to Python 3.11

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

