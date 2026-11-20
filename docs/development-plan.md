# Development Plan

Status of OctoPrint-GrblPCB as of 2026-11-20 (OctoPrint 2.0.x and `dev` 2.1.0.dev368). Replaces the original
`DEVELOPMENT_PLAN.md` that lived next to the OctoPrint checkout.

## Goal

One OctoPrint instance controls both 3D printers (standard OctoPrint) and
GRBL CNC machines for PCB isolation milling: Gerber/Excellon in, toolpaths,
autolevel and streaming out, without an external CAM tool.

## Status

"Simulator run" means exercised end to end in OctoPrint against
`tools/grbl_simulator.py`. **Nothing is verified on a real machine yet.**

| Area | Checked by |
|------|------------|
| GRBL connection type (`ConnectedPrinter`, OctoPrint 2.x) | simulator run, tests |
| Status reports, errors, alarms (shown in OctoPrint) | simulator run, tests |
| Streaming with character counting, reader thread | simulator run (RX buffer modelled), tests |
| Feed hold, resume, soft reset | simulator run |
| Real-time overrides (feed, rapid, spindle) | simulator run |
| Homing, unlock, jog, work coordinate systems, zeroing | simulator run |
| Manual commands locked while a job runs, allowed in a pause | simulator run, tests |
| Gerber RS-274X and Excellon import (LZ/TZ, file formats) | real KiCad files, tests |
| Isolation routing: climb milling (M3), overlapping passes, depth passes, narrow-gap pass per gap, warning for gaps below the tool tip and for gaps left uncut | simulator run, geometric tests |
| Drilling with peck cycles, breakthrough, tool changes per drill size | simulator run (3 tool changes), tests |
| Outline cut on the Edge.Cuts centre line, cutouts, tabs, breakthrough | geometric tests |
| Solder mask UV exposure toolpaths | simulator run, G-code inspected |
| Autolevel: probe grid, interpolation, Z correction, drift check | simulator run, tests |
| Leveled G-code bound to work zero and board side | simulator run, tests |
| Double-sided boards: back side from mirrored geometry, alignment via probed holes | simulator run end to end on the two-layer fixture, tests |
| Program pause (M0) and tool change (M6) with confirmation | simulator run, tests |
| 2D/3D workspace view | simulator run, JS test for fit view |
| German translation, translation guard test | tests |
| Development container (`docker/`) | image build |
| Job outcome: completed, stopped, failed (GRBL error, alarm, lost connection); stop keeps the position | simulator run, tests |
| Pre-flight check against the machine travel (`$130` to `$132`) | tests (placement not exercised in the simulator, which stands at its origin) |
| OctoPrint permissions for API commands and uploads | tests |
| Clear polarity in Gerber files, drilled `G85` slots | tests |
| Wide view, OctoPrint confirmation dialogs | simulator run in the browser |

## Open

- First run on the real machine: jog directions, probe wiring, a test cut on
  scrap board, then a real board.
- Camera support for fiducial alignment (original phase 4 idea).
- Tool and material presets (feeds, speeds, depths per tool).
- Job queue across operations (today each operation is sent on its own).

## Development

- Local: OctoPrint `dev` checkout in a venv, plugin installed with
  `pip install -e .`, simulator from `tools/`. Checks: see README,
  "Development Checks".
- Container: `docker compose -f docker/docker-compose.yml up --build`, with
  the simulator enabled by `GRBL_SIMULATOR=1`.
