// Run with: node tests/js/fit_view.test.js
// Checks that "fit view" keeps the whole board on screen in 2D and 3D.
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const window = {};
global.window = window;
global.gettext = (s) => s;
global._ = { sprintf: (s) => s };
global.requestAnimationFrame = () => 0;
new Function("window", fs.readFileSync(path.join(__dirname, "../../octoprint_grblpcb/static/js/workspace_renderer.js"), "utf8"))(window);

function renderer(mode, yaw, pitch) {
    const r = Object.create(window.WorkspaceRenderer.prototype);
    Object.assign(r, {
        _logicalWidth: 800, _logicalHeight: 1000, zoom: 1, panX: 0, panY: 0,
        viewMode: mode, rotation3D: { pitch: pitch, yaw: yaw },
        boardBounds: { min_x: 0, min_y: 0, max_x: 113, max_y: 253 },
        probePoints: [], toolpaths: [], layers: [], _zMin: -0.25, _zMax: 0.24,
        render: () => {},
    });
    return r;
}

const cases = [["2d", 0, 0], ["3d", -25, 35], ["3d", 40, 60], ["3d", 90, 20]];
for (const [mode, yaw, pitch] of cases) {
    const r = renderer(mode, yaw, pitch);
    r.fitToView();
    for (const x of [0, 113]) {
        for (const y of [0, 253]) {
            const p = r._w2s(x, y, 0);
            assert.ok(p.x >= 0 && p.x <= 800 && p.y >= 0 && p.y <= 1000,
                `${mode} yaw=${yaw} pitch=${pitch}: corner (${x},${y}) at (${p.x.toFixed(0)},${p.y.toFixed(0)}) is off screen`);
        }
    }
}
console.log("fit view: ok");
