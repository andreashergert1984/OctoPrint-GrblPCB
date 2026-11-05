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

> **Requires OctoPrint 1.12.0 or later.** This plugin uses the new `ConnectedPrinter` API
> which is not available in OctoPrint 1.11.x or earlier. The plugin will refuse to load
> on incompatible versions.

### From Source (Development)

```bash
cd /path/to/your/plugins
git clone https://github.com/ahergert/OctoPrint-GrblPCB.git
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

### Required OctoPrint Settings

After installing the plugin:

1. **Serial Connection → Advanced → "Hello" command**: Set to `M5`
2. **Features → Send a checksum with the command**: Set to **Never**

## Configuration

All settings are accessible from the OctoPrint Settings panel under **GrblPCB**.

| Group | Setting | Description |
|-------|---------|-------------|
| Feed | Default Jog Feed | Jog speed (mm/min) |
| Feed | Safe Z Height | Clearance height for rapids (mm) |
| Probe | Probe Depth | Max probe travel (mm, negative) |
| Probe | Probe Feed Rate | Touch-probe speed (mm/min) |
| Probe | Probe Grid Spacing | Autolevel grid resolution (mm) |
| Tool | Tool Diameter | V-bit tip diameter (mm) |
| Tool | Tool Angle | V-bit included angle (°) |
| Tool | Cut Depth | Isolation engraving depth (mm) |
| Tool | Number of Passes | Multi-pass isolation count |
| Spindle | Default Spindle Speed | RPM |
| Spindle | Startup Delay | Spin-up wait (s) |
| Drill | Peck Depth | Peck-drill increment (mm) |
| Drill | Retract Height | Peck-drill retract (mm) |
| Outline | Tab Width / Height | Workholding tab dimensions (mm) |
| Outline | Number of Tabs | Tabs per board edge |

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
├── .github/workflows/ci.yml   # CI: lint, test, build
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

## Contributing

1. Fork the repository
2. Create a feature branch
3. Submit a pull request

## Support

- [GitHub Issues](https://github.com/ahergert/OctoPrint-GrblPCB/issues)
