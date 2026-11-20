// Run with: node tests/js/toolpath_scale.test.js
// In 3D the toolpaths are drawn at true Z scale, not with the heat surface exaggeration.
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const window = {};
global.window = window;
global.gettext = (s) => s;
global._ = { sprintf: (s) => s };
global.requestAnimationFrame = () => 0;
new Function("window", fs.readFileSync(path.join(__dirname, "../../octoprint_grblpcb/static/js/workspace_renderer.js"), "utf8"))(window);

const r = Object.create(window.WorkspaceRenderer.prototype);
Object.assign(r, {
    _logicalWidth: 800, _logicalHeight: 1000, zoom: 1, panX: 0, panY: 0,
    viewMode: "3d", rotation3D: { pitch: 35, yaw: -25 },
    toolpaths: [{ visible: true, points: [
        { x: 5, y: 5, z: 0 },
        { x: 10, y: 5, z: -0.2, spindle: true },
    ] }],
});
const moves = [];
const ctx = {
    beginPath() {}, stroke() {}, setLineDash() {},
    moveTo(x, y) { moves.push(["m", x, y]); },
    lineTo(x, y) { moves.push(["l", x, y]); },
};
r._drawToolpaths(ctx);
const end = r._w2s(10, 5, -0.2);          // heat surface scale (default)
const trueScale = r._w2s(10, 5, -0.2, 1);
const drawn = moves.find((m) => m[0] === "l");
assert.ok(Math.abs(drawn[1] - trueScale.x) < 1e-9 && Math.abs(drawn[2] - trueScale.y) < 1e-9,
    "toolpath end point is not drawn at true Z scale");
assert.ok(Math.abs(drawn[2] - end.y) > 0.5, "test cannot tell the scales apart");
console.log("toolpath scale: ok");
