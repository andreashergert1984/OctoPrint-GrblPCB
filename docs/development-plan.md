# Development Plan

Status of OctoPrint-GrblPCB as of November 2026. Replaces the original
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
| Streaming with character counting, reader thread | simulator run, tests |
| Feed hold, resume, soft reset | simulator run |
| Real-time overrides (feed, rapid, spindle) | implemented, not exercised |
| Homing, unlock, jog, work coordinate systems, zeroing | simulator run |
| Gerber RS-274X and Excellon import (LZ/TZ, file formats) | real KiCad files, tests |
| Isolation routing, multi-pass, depth passes | simulator run, tests |
| Drilling with peck cycles and breakthrough | G-code inspected, tests |
| Outline cut with tabs and breakthrough | G-code inspected, tests |
| Solder mask UV exposure toolpaths | implemented, not exercised |
| Autolevel: probe grid, interpolation, Z correction, drift check | simulator run, tests |
| Leveled G-code bound to the work zero it was probed with | tests |
| Double-sided alignment via probed holes | implemented, not exercised |
| Tool change (M6) and program pause (M0) with confirmation | implemented, not exercised |
| 2D/3D workspace view | 2D seen in simulator run |
| German translation, translation guard test | tests |
| Development container (`docker/`) | image build |

## Open

- First run on the real machine: jog directions, probe wiring, a test cut on
  scrap board, then a real board.
- Exercise the parts marked "not exercised" above in the simulator, then on
  the machine.
- Camera support for fiducial alignment (original phase 4 idea).
- Tool and material presets (feeds, speeds, depths per tool).
- Job queue across operations (today each operation is sent on its own).

## Development

- Local: OctoPrint `dev` checkout in a venv, plugin installed with
  `pip install -e .`, simulator from `tools/`. Checks: see README,
  "Development Checks".
- Container: `docker compose -f docker/docker-compose.yml up --build`, with
  the simulator enabled by `GRBL_SIMULATOR=1`.
