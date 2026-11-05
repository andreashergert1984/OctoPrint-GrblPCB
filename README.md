# OctoPrint-GrblPCB

An OctoPrint plugin for PCB isolation milling on GRBL-based CNC machines. Import Gerber/Excellon files directly, generate toolpaths, autolevel your board, and mill — all from a single UI.

## Features

### Gerber & Excellon Parsing
- Import **Gerber RS-274X** files (copper, silkscreen, soldermask, outline)
- Import **Excellon** drill files with automatic unit/format detection
- Automatic geometry extraction via [pygerber](https://github.com/Argmaster/pygerber) + [Shapely](https://shapely.readthedocs.io/)
- SVG preview generation

### Toolpath Generation
- **Isolation routing** — V-bit engraving with configurable tool diameter, angle, depth, and multi-pass support
- **Drill toolpaths** — optimised drill ordering with peck-drilling support
- **Outline cutting** — board profile routing with tabs for workholding
- Full control over feed rates, spindle speed, plunge rates, and safe-Z height
- Instant G-code preview in the 3-D workspace viewer

### Autolevel (Height-Map Probing)
- Define a rectangular probe grid over the PCB
- Automatic G38.2 touch-probe cycle at each point
- Bilinear / cubic interpolation of the height map
- G-code Z-correction applied per-line for perfectly levelled traces

### Double-Sided PCB Alignment
- Define alignment (fiducial) holes on the board
- Probe each hole to measure actual position
- Least-squares rigid transform (rotation + translation) calculation
- Automatic coordinate correction for the back side

### 3-D Workspace Renderer
- Real-time Canvas-based board visualisation
- Orbit / pan / zoom navigation with mouse and touch
- Displays board outline, copper geometry, drill holes, and toolpath preview
- Colour-coded rapid vs. cutting moves

### GRBL 1.1 Integration
- Native **OctoPrint connector** — registers as a first-class connection type
- Full GRBL 1.1 protocol: `$H`, `$X`, feed hold (`!`), resume (`~`), soft reset
- Real-time status polling (state, MPos/WPos, feed, spindle)
- Work Coordinate System management (G54 – G59)
- **Character-counting streaming** for maximum throughput
- **Real-time overrides** — feed, rapid, and spindle override via single-byte commands
- Tool-change (`M6`) and pause (`M0`) handling with user confirmation

### Spindle Control
- On/Off with configurable RPM
- Startup delay for spin-up
- Min / max RPM limits

### Project Management
- Per-project directory with Gerber/Excellon source files + `project.json`
- Board rotation with stored angle (always computed from originals — no drift)
- Save / load / delete projects
- Auto-restore last project on OctoPrint startup

## Installation

> **Requires OctoPrint 2.0 and Python 3.11 or later.** The plugin uses the
> `ConnectedPrinter` API, which OctoPrint 1.11.x does not have; on such versions it
> refuses to load. Tested against the OctoPrint `dev` branch after 2.0.0rc7.

### From Source (Development)

```bash
cd /path/to/your/plugins
git clone https://git.hergert.info/octoprint/OctoPrint-GrblPCB.git
cd OctoPrint-GrblPCB
source /path/to/octoprint/venv/bin/activate
pip install -e .
```

Restart OctoPrint after installation.

### Dependencies

Installed automatically via `pip`:

| Package | Purpose |
|---------|---------|
| pygerber ≥ 2.4 | Gerber / Excellon parsing |
| pyserial ≥ 3.4 | Serial communication with GRBL |
| shapely ≥ 2.0 | 2-D geometry operations |
| scipy ≥ 1.10 | Height-map interpolation |
| numpy ≥ 1.24 | Numerical computation |

The plugin brings its own GRBL connection type ("GRBL CNC Connection"), so OctoPrint's
serial connection settings (hello command, checksums) do not apply to it.

The plugin has no automatic update check; update with `git pull` and restart OctoPrint.

## Configuration

Machine defaults live in the OctoPrint Settings panel under **GrblPCB**:

| Group | Setting | Description |
|-------|---------|-------------|
| Feed | Default Feed / Jog Feed | Cutting and jog speed (mm/min) |
| Feed | Safe Z Height | Clearance height for rapids (mm) |
| Feed | Default WCS | Work coordinate system (G54 to G59) |
| Probe | Probe Depth / Probe Feed | Max probe travel (mm, negative) and touch-probe speed |
| Tool | Tool Diameter / Tool Angle | V-bit tip diameter (mm) and included angle |
| Spindle | Default Speed, Min / Max RPM, Startup Delay | Spindle defaults |
| Machine | Soft / Hard Limits, position display | Machine options |

The per-job parameters are set in the **CNC/PCB → Platinenprojekt** tab before generating
G-code: isolation (cut depth, depth per pass, passes, overlap), speeds and heights,
drilling (feed, peck depth, board thickness, breakthrough), outline (tool, tabs).

## Usage

### Typical PCB Milling Workflow

1. **Home** the machine (`$H`)
2. **Create a new project** in the GrblPCB tab
3. **Import layers** — drag-and-drop Gerber (copper, outline) and Excellon (drill) files
4. **Rotate** the board if needed (90° increments or custom)
5. **Set work origin** — jog to the corner of your copper-clad board, probe Z, zero all axes
6. **Autolevel** — define a probe grid, run the probing cycle
7. **Generate toolpaths** — isolation routing, drilling, outline cutting
8. **Stream to CNC** — select an operation and click Send; real-time progress is shown
9. **Repeat for back side** (optional) — use the alignment feature to register the board

### Double-Sided Alignment

1. Add at least 2 alignment holes (drill reference points)
2. Drill the alignment holes on the front side
3. Flip the board and secure it
4. Probe each alignment hole on the back side
5. Compute alignment transform — the plugin calculates rotation + offset
6. All subsequent coordinates are automatically corrected

## Testing Without Hardware

`tools/grbl_simulator.py` simulates a GRBL 1.1 controller on a pseudo terminal:
real-time commands, planner buffer, status reports with `WCO`, probing against a
slightly warped surface (`[PRB:...]`), alarms.

```bash
python tools/grbl_simulator.py /tmp/ttyGRBL --verbose
```

Add the path (or a glob such as `/tmp/ttyGRBL*`) under **Settings → Serial
Connection → Additional serial ports** (`plugins.serial_connector.additionalPorts`);
the GRBL connection then lists it. `--homing` starts in `Alarm` like a machine
with `$22=1`, `--time-scale` sets how fast moves run (default 5 % of real time).
`--hole X,Y,D` (machine coordinates, repeatable) adds drilled holes whose walls
X/Y probes find, for testing double-sided alignment; `--probe-diameter` sets the
probe tip. The simulator also models GRBL's 128-byte RX buffer and prints a
warning if a sender overfills it.

At the default time scale the simulated machine finishes moves faster than the
plugin can stream them, so its planner runs empty and the status shows `Idle`
between moves, as real GRBL does when it starves. Use `--time-scale 1` to see the
status a real machine would report during a job.

### In a Container

`docker/` builds OctoPrint from source with the plugin installed editable and the
simulator enabled:

```bash
docker compose -f docker/docker-compose.yml up --build
```

Open http://localhost:5000, choose **GRBL CNC Connection** and the port
`/tmp/ttyGRBL`. Status and open points: [docs/development-plan.md](docs/development-plan.md).

## Development Checks

There is no CI pipeline; run the checks locally in the OctoPrint venv before pushing:

```bash
python -m ruff check octoprint_grblpcb tools tests
python -m pytest
```

After changing user-facing texts, update the German translation:

```bash
python setup.py babel_extract && python setup.py babel_refresh
# translate new entries in translations/de/LC_MESSAGES/messages.po
python setup.py babel_compile && python setup.py babel_bundle --locale de
```

`tests/test_translations.py` fails while any text is untranslated or the bundled
copy is out of date.

## Plugin Structure

```
OctoPrint-GrblPCB/
├── octoprint_grblpcb/
│   ├── __init__.py            # Plugin entry, API handlers, probe loops
│   ├── connector.py           # OctoPrint GRBL connector + streaming
│   ├── grbl_comm.py           # Low-level serial GRBL communication
│   ├── gerber_parser.py       # Gerber/Excellon → Shapely geometry
│   ├── toolpath.py            # Geometry → G-code toolpath generation
│   ├── pcb_project.py         # Project data model & persistence
│   ├── autolevel.py           # Height-map probing & Z-correction
│   ├── static/
│   │   ├── css/grblpcb.css
│   │   └── js/
│   │       ├── grblpcb.js             # Knockout.js view-model
│   │       └── workspace_renderer.js  # 3-D canvas renderer
│   ├── templates/
│   │   ├── grblpcb_connection_option.jinja2
│   │   ├── grblpcb_navbar.jinja2
│   │   ├── grblpcb_settings.jinja2
│   │   └── grblpcb_tab.jinja2
│   └── translations/          # Bundled i18n files
├── tests/                     # Automated test suite (pytest)
├── tools/grbl_simulator.py    # GRBL 1.1 simulator for testing without hardware
├── docker/                    # Development container (OctoPrint from source + simulator)
├── docs/development-plan.md   # Status and open points
├── babel.cfg                  # i18n string extraction config
├── CHANGELOG.md
├── pyproject.toml
├── LICENSE                    # AGPL-3.0
└── README.md
```

## GRBL Commands Reference

| Command | Description |
|---------|-------------|
| `$H` | Home all axes |
| `$X` | Unlock alarm state |
| `Ctrl-X` | Soft reset |
| `!` | Feed hold (pause) |
| `~` | Resume from hold |
| `?` | Request status |
| `G38.2` | Probe towards workpiece |
| `G10 L20` | Set work offset |

## License

This plugin is licensed under the [GNU Affero General Public License v3.0](LICENSE).

## Credits

- [OctoPrint](https://octoprint.org/) by Gina Häußge
- [pygerber](https://github.com/Argmaster/pygerber) by Krzysztof Wiśniewski
- Inspired by [octoprint-grbl-plugin](https://github.com/mic159/octoprint-grbl-plugin) by mic159

## Issues

- [https://git.hergert.info/octoprint/OctoPrint-GrblPCB/-/issues](https://git.hergert.info/octoprint/OctoPrint-GrblPCB/-/issues)
