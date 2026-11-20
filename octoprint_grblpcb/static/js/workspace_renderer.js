/*
 * OctoPrint-GrblPCB - Workspace Renderer
 *
 * Canvas-based 2D/3D PCB workspace visualizer.
 * Shows board outline, probe grid, height map surface,
 * G-code toolpaths, and machine position in a unified view.
 */

(function(global) {
    "use strict";

    // =========================================================================
    // Color utilities
    // =========================================================================

    function heatColor(value, min, max) {
        // Maps a value in [min..max] to a blue-green-yellow-red gradient
        if (max === min) return "rgba(128,128,128,0.6)";
        var t = Math.max(0, Math.min(1, (value - min) / (max - min)));
        var r, g, b;
        if (t < 0.25) {
            r = 0; g = Math.round(255 * (t / 0.25)); b = 255;
        } else if (t < 0.5) {
            r = 0; g = 255; b = Math.round(255 * (1 - (t - 0.25) / 0.25));
        } else if (t < 0.75) {
            r = Math.round(255 * ((t - 0.5) / 0.25)); g = 255; b = 0;
        } else {
            r = 255; g = Math.round(255 * (1 - (t - 0.75) / 0.25)); b = 0;
        }
        return "rgba(" + r + "," + g + "," + b + ",0.85)";
    }

    function heatColorSolid(value, min, max) {
        if (max === min) return "#808080";
        var t = Math.max(0, Math.min(1, (value - min) / (max - min)));
        var r, g, b;
        if (t < 0.25) {
            r = 0; g = Math.round(255 * (t / 0.25)); b = 255;
        } else if (t < 0.5) {
            r = 0; g = 255; b = Math.round(255 * (1 - (t - 0.25) / 0.25));
        } else if (t < 0.75) {
            r = Math.round(255 * ((t - 0.5) / 0.25)); g = 255; b = 0;
        } else {
            r = 255; g = Math.round(255 * (1 - (t - 0.75) / 0.25)); b = 0;
        }
        return "rgb(" + r + "," + g + "," + b + ")";
    }

    // =========================================================================
    // WorkspaceRenderer class
    // =========================================================================

    function WorkspaceRenderer(canvasId) {
        this.canvas = document.getElementById(canvasId);
        if (!this.canvas) {
            console.error("WorkspaceRenderer: canvas not found:", canvasId);
            return;
        }
        this.ctx = this.canvas.getContext("2d");

        // Data sources (set externally)
        this.boardBounds = null;     // {min_x, min_y, max_x, max_y}
        this.probePoints = [];       // [{x, y, z, probed}, ...]
        this.gridSize = {x: 0, y: 0}; // grid dimensions
        this.toolpaths = [];         // [{points: [{x,y,z},...], color: "#..."}, ...]
        this.machinePos = null;      // {x, y, z}
        this.layers = [];            // [{layer_type, min_x, min_y, max_x, max_y}, ...]
        this.gerberPolygons = null;  // { layer_type: [{exterior:[[x,y],...], interiors:[...]}, ...] }
        this.drillHoles = [];        // [[x, y, diameter], ...]
        this._viewInitialized = false;

        // View state
        this.viewMode = "2d";        // "2d" or "3d"
        this.panX = 0;
        this.panY = 0;
        this.zoom = 1;
        this.rotation3D = {pitch: 35, yaw: -25};  // degrees for 3D view

        // Layer visibility
        this.showBoard = true;
        this.showProbeGrid = true;
        this.showHeightmap = true;
        this.showToolpaths = true;
        this.showMachinePos = true;
        this.showAxes = true;
        this.showHeatSurface = true;
        this.showLayers = true;
        this.showGerberPolygons = true;

        // Board thickness for 3D extrusion (mm)
        this.boardThickness = 1.6;

        // Height map range (computed)
        this._zMin = 0;
        this._zMax = 0;

        // Interaction state
        this._dragging = false;
        this._rotating = false;
        this._lastMouse = {x: 0, y: 0};
        this._mouseWorld = null;  // {x, y} in world coords for cursor display
        this._animFrame = null;

        // Bind event handlers
        this._onMouseDown = this._handleMouseDown.bind(this);
        this._onMouseMove = this._handleMouseMove.bind(this);
        this._onMouseUp = this._handleMouseUp.bind(this);
        this._onMouseLeave = this._handleMouseLeave.bind(this);
        this._onWheel = this._handleWheel.bind(this);
        this._onContextMenu = function(e) { e.preventDefault(); };
        this._onResize = this._handleResize.bind(this);

        this._attachEvents();
        this._handleResize();
    }

    // =========================================================================
    // Event handling
    // =========================================================================

    WorkspaceRenderer.prototype._attachEvents = function() {
        this.canvas.addEventListener("mousedown", this._onMouseDown);
        this.canvas.addEventListener("mousemove", this._onMouseMove);
        this.canvas.addEventListener("mouseup", this._onMouseUp);
        this.canvas.addEventListener("mouseleave", this._onMouseLeave);
        this.canvas.addEventListener("wheel", this._onWheel, {passive: false});
        this.canvas.addEventListener("contextmenu", this._onContextMenu);
        window.addEventListener("resize", this._onResize);
        // The canvas also changes size when the control panel is toggled
        if (typeof ResizeObserver === "function" && this.canvas.parentElement) {
            this._resizeObserver = new ResizeObserver(this._onResize);
            this._resizeObserver.observe(this.canvas.parentElement);
        }
    };

    WorkspaceRenderer.prototype.destroy = function() {
        this.canvas.removeEventListener("mousedown", this._onMouseDown);
        this.canvas.removeEventListener("mousemove", this._onMouseMove);
        this.canvas.removeEventListener("mouseup", this._onMouseUp);
        this.canvas.removeEventListener("mouseleave", this._onMouseLeave);
        this.canvas.removeEventListener("wheel", this._onWheel);
        this.canvas.removeEventListener("contextmenu", this._onContextMenu);
        window.removeEventListener("resize", this._onResize);
        if (this._resizeObserver) this._resizeObserver.disconnect();
        if (this._animFrame) cancelAnimationFrame(this._animFrame);
    };

    WorkspaceRenderer.prototype._handleResize = function() {
        var container = this.canvas.parentElement;
        if (!container) return;
        var rect = container.getBoundingClientRect();
        var dpr = window.devicePixelRatio || 1;
        this.canvas.width = rect.width * dpr;
        this.canvas.height = rect.height * dpr;
        this.canvas.style.width = rect.width + "px";
        this.canvas.style.height = rect.height + "px";
        this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        this._logicalWidth = rect.width;
        this._logicalHeight = rect.height;
        if (!this._viewInitialized && rect.width > 0 && rect.height > 0) {
            this._setDefaultView();
        } else if (this._autoFit && rect.width > 0 && rect.height > 0) {
            // The view still shows what "fit" chose: keep it fitted when
            // the panel changes size (wide view, side bar, window)
            this.fitToView();
            return;
        }
        this.render();
    };

    // Without data: show the origin near the bottom left at a scale where
    // a typical board (100 x 80 mm) fits
    WorkspaceRenderer.prototype._setDefaultView = function() {
        var w = this._logicalWidth, h = this._logicalHeight;
        if (!w || !h) return;
        this.zoom = Math.max(0.5, Math.min((w - 120) / 100, (h - 120) / 80));
        this.panX = 60;
        this.panY = h - 60;
        this._viewInitialized = true;
    };

    WorkspaceRenderer.prototype._handleMouseDown = function(e) {
        e.preventDefault();
        this._lastMouse = {x: e.clientX, y: e.clientY};
        if (this.viewMode === "3d") {
            if (e.button === 0 && !e.shiftKey) {
                this._rotating = true;   // left-drag: rotate
            } else if (e.button === 2 || e.button === 1 || (e.button === 0 && e.shiftKey)) {
                this._dragging = true;   // right/middle/shift+left: pan
            }
        } else {
            if (e.button === 0 || e.button === 1) {
                this._dragging = true;   // left or middle: pan
            }
        }
    };

    WorkspaceRenderer.prototype._handleMouseMove = function(e) {
        // Update cursor world coordinates (2D mode only for now)
        if (this.viewMode === "2d") {
            var rect = this.canvas.getBoundingClientRect();
            var sx = e.clientX - rect.left;
            var sy = e.clientY - rect.top;
            this._mouseWorld = this._screenToWorld2D(sx, sy);
        } else {
            this._mouseWorld = null;
        }

        if (!this._dragging && !this._rotating) {
            // Throttled render for cursor display only
            this._scheduleRender();
            return;
        }
        var dx = e.clientX - this._lastMouse.x;
        var dy = e.clientY - this._lastMouse.y;
        this._lastMouse = {x: e.clientX, y: e.clientY};

        if (this._rotating && this.viewMode === "3d") {
            this.rotation3D.yaw -= dx * 0.2;
            this.rotation3D.pitch = Math.max(5, Math.min(85, this.rotation3D.pitch - dy * 0.2));
        } else if (this._dragging) {
            this.panX += dx;
            this.panY += dy;
        }
        if (dx !== 0 || dy !== 0) this._autoFit = false;
        this.render();
    };

    WorkspaceRenderer.prototype._handleMouseUp = function() {
        this._dragging = false;
        this._rotating = false;
    };

    WorkspaceRenderer.prototype._handleMouseLeave = function() {
        this._dragging = false;
        this._rotating = false;
        this._mouseWorld = null;
        this.render();
    };

    WorkspaceRenderer.prototype._handleWheel = function(e) {
        e.preventDefault();
        this._autoFit = false;
        var factor = e.deltaY > 0 ? 0.9 : 1.1;
        // Zoom toward mouse position
        var rect = this.canvas.getBoundingClientRect();
        var mx = e.clientX - rect.left;
        var my = e.clientY - rect.top;
        this.panX = mx - (mx - this.panX) * factor;
        this.panY = my - (my - this.panY) * factor;
        this.zoom *= factor;
        this.render();
    };

    // =========================================================================
    // View management
    // =========================================================================

    WorkspaceRenderer.prototype.fitToView = function() {
        var bounds = this._getWorldBounds();
        if (!bounds) {
            this._setDefaultView();
            this.render();
            return;
        }
        this._viewInitialized = true;
        this._autoFit = true;

        var worldW = bounds.max_x - bounds.min_x;
        var worldH = bounds.max_y - bounds.min_y;
        if (worldW <= 0 || worldH <= 0) return;

        var padding = 40; // px
        var canvasW = this._logicalWidth - padding * 2;
        var canvasH = this._logicalHeight - padding * 2;

        if (this.viewMode === "3d") {
            // Fit the projected corners: rotation and tilt change the
            // on-screen extent of the board
            var minSX = Infinity, maxSX = -Infinity, minSY = Infinity, maxSY = -Infinity;
            var xs = [bounds.min_x, bounds.max_x], ys = [bounds.min_y, bounds.max_y];
            var zs = [this._zMin || 0, this._zMax || 0];
            for (var i = 0; i < 2; i++) {
                for (var j = 0; j < 2; j++) {
                    for (var k = 0; k < 2; k++) {
                        var p = this._project3D(xs[i], ys[j], zs[k]);
                        minSX = Math.min(minSX, p.x); maxSX = Math.max(maxSX, p.x);
                        minSY = Math.min(minSY, p.y); maxSY = Math.max(maxSY, p.y);
                    }
                }
            }
            this.zoom = Math.min(canvasW / (maxSX - minSX || 1), canvasH / (maxSY - minSY || 1));
            this.panX = this._logicalWidth / 2 - (minSX + maxSX) / 2 * this.zoom;
            this.panY = this._logicalHeight / 2 + (minSY + maxSY) / 2 * this.zoom;
            this.render();
            return;
        }

        this.zoom = Math.min(canvasW / worldW, canvasH / worldH);

        var cx = (bounds.min_x + bounds.max_x) / 2;
        var cy = (bounds.min_y + bounds.max_y) / 2;
        this.panX = this._logicalWidth / 2 - cx * this.zoom;
        this.panY = this._logicalHeight / 2 + cy * this.zoom;  // Y flipped

        this.render();
    };

    WorkspaceRenderer.prototype._getWorldBounds = function() {
        var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        var hasData = false;

        if (this.boardBounds) {
            minX = Math.min(minX, this.boardBounds.min_x);
            minY = Math.min(minY, this.boardBounds.min_y);
            maxX = Math.max(maxX, this.boardBounds.max_x);
            maxY = Math.max(maxY, this.boardBounds.max_y);
            hasData = true;
        }

        for (var i = 0; i < this.probePoints.length; i++) {
            var p = this.probePoints[i];
            minX = Math.min(minX, p.x);
            minY = Math.min(minY, p.y);
            maxX = Math.max(maxX, p.x);
            maxY = Math.max(maxY, p.y);
            hasData = true;
        }

        for (var i = 0; i < this.toolpaths.length; i++) {
            var tp = this.toolpaths[i];
            for (var j = 0; j < tp.points.length; j++) {
                minX = Math.min(minX, tp.points[j].x);
                minY = Math.min(minY, tp.points[j].y);
                maxX = Math.max(maxX, tp.points[j].x);
                maxY = Math.max(maxY, tp.points[j].y);
                hasData = true;
            }
        }

        // Include gerber layers
        for (var i = 0; i < this.layers.length; i++) {
            var layer = this.layers[i];
            if (layer.visible && layer.width > 0) {
                minX = Math.min(minX, layer.min_x);
                minY = Math.min(minY, layer.min_y);
                maxX = Math.max(maxX, layer.max_x);
                maxY = Math.max(maxY, layer.max_y);
                hasData = true;
            }
        }

        if (!hasData) return null;
        // Add small margin
        var margin = Math.max(maxX - minX, maxY - minY) * 0.05;
        return {
            min_x: minX - margin, min_y: minY - margin,
            max_x: maxX + margin, max_y: maxY + margin
        };
    };

    WorkspaceRenderer.prototype.setViewMode = function(mode) {
        this.viewMode = mode;
        if (mode === "3d") {
            this.rotation3D = {pitch: 35, yaw: -25};
        }
        this.fitToView();
    };

    // =========================================================================
    // Coordinate transforms
    // =========================================================================

    WorkspaceRenderer.prototype._worldToScreen2D = function(wx, wy) {
        return {
            x: wx * this.zoom + this.panX,
            y: -wy * this.zoom + this.panY  // flip Y axis (CNC Y+ is up)
        };
    };

    WorkspaceRenderer.prototype._screenToWorld2D = function(sx, sy) {
        return {
            x: (sx - this.panX) / this.zoom,
            y: -(sy - this.panY) / this.zoom
        };
    };

    WorkspaceRenderer.prototype._project3D = function(wx, wy, wz, zScaleOverride) {
        // Projection without zoom and pan; x right, y up
        var p = this._worldToScreen3D(wx, wy, wz, zScaleOverride, true);
        return {x: p.x, y: -p.y};
    };

    WorkspaceRenderer.prototype._worldToScreen3D = function(wx, wy, wz, zScaleOverride, unitView) {
        // Isometric-like projection with configurable rotation
        var pitchRad = this.rotation3D.pitch * Math.PI / 180;
        var yawRad = this.rotation3D.yaw * Math.PI / 180;

        // Z scale: default 15x exaggeration for height-map visibility,
        // but callers can pass zScaleOverride=1 for true-scale rendering
        // of board thickness, toolpaths, machine position etc.
        var zScale = (typeof zScaleOverride === "number") ? zScaleOverride : 15.0;
        var scaledZ = -wz * zScale;

        // Rotate around Y axis (yaw) — only affects X/Y plane
        var rx = wx * Math.cos(yawRad) + wy * Math.sin(yawRad);
        var ry = wy * Math.cos(yawRad) - wx * Math.sin(yawRad);

        // Rotate around X axis (pitch) — tilts Y/Z to create 3D perspective
        var sy = ry * Math.cos(pitchRad) - scaledZ * Math.sin(pitchRad);
        var sz = ry * Math.sin(pitchRad) + scaledZ * Math.cos(pitchRad);

        var zoom = unitView ? 1 : this.zoom;
        return {
            x: rx * zoom + (unitView ? 0 : this.panX),
            y: -sy * zoom + (unitView ? 0 : this.panY),
            depth: sz
        };
    };

    WorkspaceRenderer.prototype._w2s = function(wx, wy, wz, zScaleOverride) {
        if (this.viewMode === "3d") {
            return this._worldToScreen3D(wx, wy, wz || 0, zScaleOverride);
        }
        return this._worldToScreen2D(wx, wy);
    };

    WorkspaceRenderer.prototype._scheduleRender = function() {
        if (this._animFrame) return;
        var self = this;
        this._animFrame = requestAnimationFrame(function() {
            self._animFrame = null;
            self.render();
        });
    };

    // =========================================================================
    // Main render
    // =========================================================================

    WorkspaceRenderer.prototype.render = function() {
        if (!this.ctx) return;
        var ctx = this.ctx;
        var w = this._logicalWidth;
        var h = this._logicalHeight;

        // Clear
        ctx.clearRect(0, 0, w, h);

        // Background
        ctx.fillStyle = "#1a1a2e";
        ctx.fillRect(0, 0, w, h);

        // Subtle grid
        this._drawGrid(ctx, w, h);

        // Update Z range
        this._computeZRange();

        // Draw layers in order (back to front for 3D)
        if (this.showLayers) this._drawGerberLayers(ctx);
        if (this.showGerberPolygons) this._drawGerberPolygons(ctx);
        if (this.showGerberPolygons) this._drawDrillHoles(ctx);
        if (this.showBoard) this._drawBoard(ctx);
        if (this.showHeatSurface && this.probePoints.length > 0) this._drawHeatSurface(ctx);
        if (this.showToolpaths) this._drawToolpaths(ctx);
        if (this.showProbeGrid) this._drawProbePoints(ctx);
        if (this.showAxes) this._drawAxes(ctx, w, h);
        if (this.showMachinePos && this.machinePos) this._drawMachinePosition(ctx);
        this._drawLabels(ctx);

        // UI overlays
        this._drawLegend(ctx, w, h);
        this._drawInfo(ctx, w, h);
        this._drawCursorCoords(ctx, w, h);
    };

    // =========================================================================
    // Draw: background grid
    // =========================================================================

    WorkspaceRenderer.prototype._drawGrid = function(ctx, w, h) {
        // Calculate grid spacing in world coords
        var gridSpacing = 10;  // mm
        if (this.zoom > 8) gridSpacing = 1;
        else if (this.zoom > 3) gridSpacing = 5;
        else if (this.zoom < 0.5) gridSpacing = 50;

        ctx.strokeStyle = "rgba(255,255,255,0.06)";
        ctx.lineWidth = 0.5;

        // Find visible world range
        var topLeft = {
            x: -this.panX / this.zoom,
            y: (this.panY) / this.zoom
        };
        var bottomRight = {
            x: (w - this.panX) / this.zoom,
            y: (this.panY - h) / this.zoom
        };

        var startX = Math.floor(topLeft.x / gridSpacing) * gridSpacing;
        var startY = Math.floor(bottomRight.y / gridSpacing) * gridSpacing;

        ctx.beginPath();
        for (var x = startX; x <= bottomRight.x; x += gridSpacing) {
            var sx = x * this.zoom + this.panX;
            ctx.moveTo(sx, 0);
            ctx.lineTo(sx, h);
        }
        for (var y = startY; y <= topLeft.y; y += gridSpacing) {
            var sy = -y * this.zoom + this.panY;
            ctx.moveTo(0, sy);
            ctx.lineTo(w, sy);
        }
        ctx.stroke();
    };

    // =========================================================================
    // Draw: axes
    // =========================================================================

    WorkspaceRenderer.prototype._drawAxes = function(ctx, w, h) {
        var origin = this._w2s(0, 0, 0);

        // Axis length: use a mix of world-space and screen-space so it stays
        // visible at any zoom but also scales reasonably.
        // Base length = 25mm in world-space, but at least 60px on screen.
        var worldLen = 25;
        var screenLen = worldLen * this.zoom;
        if (screenLen < 60) worldLen = 60 / this.zoom;

        var xEnd = this._w2s(worldLen, 0, 0);
        var yEnd = this._w2s(0, worldLen, 0);

        var arrowSize = 8; // px

        // Helper: draw arrowhead at tip of axis line
        var drawArrow = function(ox, oy, tx, ty) {
            var dx = tx - ox, dy = ty - oy;
            var len = Math.sqrt(dx * dx + dy * dy);
            if (len < 1) return;
            var ux = dx / len, uy = dy / len;
            var px = -uy, py = ux; // perpendicular
            ctx.beginPath();
            ctx.moveTo(tx, ty);
            ctx.lineTo(tx - ux * arrowSize + px * arrowSize * 0.4,
                       ty - uy * arrowSize + py * arrowSize * 0.4);
            ctx.lineTo(tx - ux * arrowSize - px * arrowSize * 0.4,
                       ty - uy * arrowSize - py * arrowSize * 0.4);
            ctx.closePath();
            ctx.fill();
        };

        // Helper: draw label with dark background for contrast
        var drawLabel = function(text, x, y, color) {
            ctx.font = "bold 13px sans-serif";
            var tw = ctx.measureText(text).width;
            ctx.fillStyle = "rgba(0,0,0,0.6)";
            ctx.fillRect(x - 2, y - 13, tw + 4, 16);
            ctx.fillStyle = color;
            ctx.fillText(text, x, y);
        };

        // X axis — red
        ctx.strokeStyle = "#ff4444";
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        ctx.moveTo(origin.x, origin.y);
        ctx.lineTo(xEnd.x, xEnd.y);
        ctx.stroke();
        ctx.fillStyle = "#ff4444";
        drawArrow(origin.x, origin.y, xEnd.x, xEnd.y);
        drawLabel("X", xEnd.x + 6, xEnd.y + 5, "#ff4444");

        // Y axis — green
        ctx.strokeStyle = "#44ff44";
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        ctx.moveTo(origin.x, origin.y);
        ctx.lineTo(yEnd.x, yEnd.y);
        ctx.stroke();
        ctx.fillStyle = "#44ff44";
        drawArrow(origin.x, origin.y, yEnd.x, yEnd.y);
        drawLabel("Y", yEnd.x + 6, yEnd.y + 5, "#44ff44");

        // Z axis (3D only) — blue
        if (this.viewMode === "3d") {
            var zEnd = this._w2s(0, 0, 8 / this.zoom, 1);
            ctx.strokeStyle = "#6666ff";
            ctx.lineWidth = 2.5;
            ctx.beginPath();
            ctx.moveTo(origin.x, origin.y);
            ctx.lineTo(zEnd.x, zEnd.y);
            ctx.stroke();
            ctx.fillStyle = "#6666ff";
            drawArrow(origin.x, origin.y, zEnd.x, zEnd.y);
            drawLabel("Z", zEnd.x + 6, zEnd.y + 5, "#6666ff");
        }

        // Origin circle + "0,0" label
        ctx.strokeStyle = "#ffffff";
        ctx.lineWidth = 2;
        ctx.fillStyle = "rgba(255,255,255,0.15)";
        ctx.beginPath();
        ctx.arc(origin.x, origin.y, 7, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
        // inner dot
        ctx.fillStyle = "#ffffff";
        ctx.beginPath();
        ctx.arc(origin.x, origin.y, 2.5, 0, Math.PI * 2);
        ctx.fill();

        drawLabel("0,0" + (this.viewMode === "3d" ? ",0" : ""),
                  origin.x + 10, origin.y + 16, "#ffffff");
    };

    // =========================================================================
    // Draw: gerber layer outlines
    // =========================================================================

    WorkspaceRenderer.prototype._layerColors = {
        "top_copper":    { fill: "rgba(0, 200, 80, 0.12)", stroke: "#00cc55" },
        "bottom_copper": { fill: "rgba(60, 120, 255, 0.12)", stroke: "#3c78ff" },
        "drill":         { fill: "rgba(255, 160, 0, 0.08)", stroke: "#ffa000" },
        "outline":       { fill: "rgba(255, 60, 60, 0.08)", stroke: "#ff4444" },
        "top_silk":      { fill: "rgba(255, 255, 100, 0.06)", stroke: "#cccc44" },
        "bottom_silk":   { fill: "rgba(180, 120, 255, 0.06)", stroke: "#b478ff" },
        "top_mask":      { fill: "rgba(200, 50, 200, 0.06)", stroke: "#c832c8" },
        "bottom_mask":   { fill: "rgba(50, 200, 200, 0.06)", stroke: "#32c8c8" }
    };

    WorkspaceRenderer.prototype._getLayerStyle = function(layerType) {
        var style = this._layerColors[layerType] || {
            fill: "rgba(180, 180, 180, 0.08)",
            stroke: "#aaaaaa"
        };
        return {
            fill: style.fill,
            stroke: style.stroke,
            label: GrblPcbLabels.layer(layerType)
        };
    };

    WorkspaceRenderer.prototype._drawGerberLayers = function(ctx) {
        if (!this.layers || this.layers.length === 0) return;

        for (var i = 0; i < this.layers.length; i++) {
            var layer = this.layers[i];
            if (!layer.visible) continue;
            if (layer.width <= 0 || layer.height <= 0) continue;

            var style = this._getLayerStyle(layer.layer_type);

            var corners;
            if (this.viewMode === "3d") {
                corners = [
                    this._w2s(layer.min_x, layer.min_y, 0),
                    this._w2s(layer.max_x, layer.min_y, 0),
                    this._w2s(layer.max_x, layer.max_y, 0),
                    this._w2s(layer.min_x, layer.max_y, 0)
                ];
            } else {
                corners = [
                    this._w2s(layer.min_x, layer.min_y),
                    this._w2s(layer.max_x, layer.min_y),
                    this._w2s(layer.max_x, layer.max_y),
                    this._w2s(layer.min_x, layer.max_y)
                ];
            }

            // Fill
            ctx.fillStyle = style.fill;
            ctx.beginPath();
            ctx.moveTo(corners[0].x, corners[0].y);
            for (var j = 1; j < 4; j++) {
                ctx.lineTo(corners[j].x, corners[j].y);
            }
            ctx.closePath();
            ctx.fill();

            // Outline
            ctx.strokeStyle = style.stroke;
            ctx.lineWidth = 1.5;
            ctx.setLineDash([4, 2]);
            ctx.stroke();
            ctx.setLineDash([]);
        }
    };

    // =========================================================================
    // Draw: board and layer labels, stacked above the board
    // =========================================================================

    WorkspaceRenderer.prototype._drawLabels = function(ctx) {
        var lines = [];
        var b = this.boardBounds;
        if (this.showBoard && b) {
            lines.push({
                text: _.sprintf(gettext("Board: %s x %s mm"),
                    (b.max_x - b.min_x).toFixed(1), (b.max_y - b.min_y).toFixed(1)),
                color: "#00cc66",
                font: "11px sans-serif"
            });
        }
        if (this.showLayers && this.layers) {
            for (var i = 0; i < this.layers.length; i++) {
                var layer = this.layers[i];
                if (!layer.visible || layer.width <= 0 || layer.height <= 0) continue;
                var style = this._getLayerStyle(layer.layer_type);
                lines.push({
                    text: style.label + " " + layer.width.toFixed(1) + " × " + layer.height.toFixed(1),
                    color: style.stroke,
                    font: "10px sans-serif"
                });
            }
        }
        if (!lines.length || !b) return;

        // Anchor: centre of the top edge of the board, or of the probe grid
        // when it reaches further (its points carry their own Z labels).
        // The last line sits just above, earlier lines stack upwards.
        var top = b.max_y;
        var gap = 6;
        if (this.showProbeGrid && this.probePoints.length) {
            for (var k = 0; k < this.probePoints.length; k++) {
                top = Math.max(top, this.probePoints[k].y);
            }
            gap = 22;  // clear the probe point marker and its Z value
        }
        var anchor = this._w2s((b.min_x + b.max_x) / 2, top, 0);
        var lineHeight = 13;
        ctx.textAlign = "center";
        for (var j = 0; j < lines.length; j++) {
            ctx.fillStyle = lines[j].color;
            ctx.font = lines[j].font;
            ctx.fillText(lines[j].text, anchor.x, anchor.y - gap - (lines.length - 1 - j) * lineHeight);
        }
        ctx.textAlign = "left";
    };

    // =========================================================================
    // Draw: Gerber copper / trace polygons
    // =========================================================================

    WorkspaceRenderer.prototype._gerberPolyColors = {
        "top_copper":    { fill: "rgba(0, 200, 80, 0.40)", stroke: "rgba(0, 200, 80, 0.85)" },
        "bottom_copper": { fill: "rgba(60, 120, 255, 0.35)", stroke: "rgba(60, 120, 255, 0.7)" },
        "outline":       { fill: "rgba(255, 60, 60, 0.20)", stroke: "rgba(255, 60, 60, 0.6)" },
        "top_silk":      { fill: "rgba(255, 255, 100, 0.15)", stroke: "rgba(255, 255, 100, 0.5)" },
        "bottom_silk":   { fill: "rgba(180, 120, 255, 0.15)", stroke: "rgba(180, 120, 255, 0.5)" },
        "top_mask":      { fill: "rgba(200, 50, 200, 0.15)", stroke: "rgba(200, 50, 200, 0.5)" },
        "bottom_mask":   { fill: "rgba(50, 200, 200, 0.15)", stroke: "rgba(50, 200, 200, 0.5)" }
    };

    WorkspaceRenderer.prototype._isLayerVisible = function(layerType) {
        for (var i = 0; i < this.layers.length; i++) {
            if (this.layers[i].layer_type === layerType) return this.layers[i].visible !== false;
        }
        return true;
    };

    WorkspaceRenderer.prototype._drawDrillHoles = function(ctx) {
        if (!this.drillHoles || this.drillHoles.length === 0) return;
        if (!this._isLayerVisible("drill")) return;
        var style = this._layerColors.drill;
        ctx.fillStyle = "rgba(10, 10, 20, 0.85)";
        ctx.strokeStyle = style.stroke;
        ctx.lineWidth = 1;
        for (var i = 0; i < this.drillHoles.length; i++) {
            var hole = this.drillHoles[i];
            var c = this._w2s(hole[0], hole[1], 0);
            var r = Math.max(1.5, hole[2] / 2 * this.zoom);
            ctx.beginPath();
            if (this.viewMode === "3d") {
                // Circles become ellipses in the tilted view; approximate
                // with a polygon through projected points
                for (var k = 0; k <= 16; k++) {
                    var a = k / 16 * 2 * Math.PI;
                    var p = this._w2s(hole[0] + Math.cos(a) * hole[2] / 2,
                                      hole[1] + Math.sin(a) * hole[2] / 2, 0);
                    if (k === 0) ctx.moveTo(p.x, p.y); else ctx.lineTo(p.x, p.y);
                }
                ctx.closePath();
            } else {
                ctx.arc(c.x, c.y, r, 0, 2 * Math.PI);
            }
            ctx.fill();
            ctx.stroke();
        }
    };

    WorkspaceRenderer.prototype._drawGerberPolygons = function(ctx) {
        if (!this.gerberPolygons) return;

        for (var layerType in this.gerberPolygons) {
            var polys = this.gerberPolygons[layerType];
            if (!polys || polys.length === 0) continue;
            if (!this._isLayerVisible(layerType)) continue;

            var colors = this._gerberPolyColors[layerType] || {
                fill: "rgba(180, 180, 180, 0.25)",
                stroke: "rgba(180, 180, 180, 0.6)"
            };

            ctx.fillStyle = colors.fill;
            ctx.strokeStyle = colors.stroke;
            ctx.lineWidth = Math.max(0.5, 0.8 / Math.sqrt(this.zoom));

            // Draw all polygons for this layer in one pass for performance
            ctx.beginPath();
            for (var i = 0; i < polys.length; i++) {
                var poly = polys[i];
                var ext = poly.exterior;
                if (!ext || ext.length < 3) continue;

                // Exterior ring
                var s = this._w2s(ext[0][0], ext[0][1]);
                ctx.moveTo(s.x, s.y);
                for (var j = 1; j < ext.length; j++) {
                    s = this._w2s(ext[j][0], ext[j][1]);
                    ctx.lineTo(s.x, s.y);
                }
                ctx.closePath();

                // Interior rings (holes)
                if (poly.interiors) {
                    for (var k = 0; k < poly.interiors.length; k++) {
                        var hole = poly.interiors[k];
                        if (!hole || hole.length < 3) continue;
                        var h = this._w2s(hole[0][0], hole[0][1]);
                        ctx.moveTo(h.x, h.y);
                        for (var m = 1; m < hole.length; m++) {
                            h = this._w2s(hole[m][0], hole[m][1]);
                            ctx.lineTo(h.x, h.y);
                        }
                        ctx.closePath();
                    }
                }
            }
            ctx.fill("evenodd");
            ctx.stroke();
        }
    };

    // =========================================================================
    // Draw: board outline
    // =========================================================================

    WorkspaceRenderer.prototype._drawBoard = function(ctx) {
        if (!this.boardBounds) return;
        var b = this.boardBounds;

        if (this.viewMode === "3d") {
            this._drawBoard3D(ctx, b);
        } else {
            this._drawBoard2D(ctx, b);
        }
    };

    WorkspaceRenderer.prototype._drawBoard2D = function(ctx, b) {
        var corners = [
            this._w2s(b.min_x, b.min_y),
            this._w2s(b.max_x, b.min_y),
            this._w2s(b.max_x, b.max_y),
            this._w2s(b.min_x, b.max_y)
        ];

        // Board fill
        ctx.fillStyle = "rgba(0, 80, 0, 0.25)";
        ctx.beginPath();
        ctx.moveTo(corners[0].x, corners[0].y);
        for (var i = 1; i < 4; i++) {
            ctx.lineTo(corners[i].x, corners[i].y);
        }
        ctx.closePath();
        ctx.fill();

        // Board outline
        ctx.strokeStyle = "#00cc66";
        ctx.lineWidth = 2;
        ctx.setLineDash([6, 3]);
        ctx.stroke();
        ctx.setLineDash([]);

    };

    WorkspaceRenderer.prototype._drawBoard3D = function(ctx, b) {
        var t = this.boardThickness || 1.6;
        var zTop = 0;
        var zBot = -t;

        // Use zScale=1 for true-scale board thickness rendering
        var t0 = this._w2s(b.min_x, b.min_y, zTop, 1);
        var t1 = this._w2s(b.max_x, b.min_y, zTop, 1);
        var t2 = this._w2s(b.max_x, b.max_y, zTop, 1);
        var t3 = this._w2s(b.min_x, b.max_y, zTop, 1);

        // Bottom face corners
        var b0 = this._w2s(b.min_x, b.min_y, zBot, 1);
        var b1 = this._w2s(b.max_x, b.min_y, zBot, 1);
        var b2 = this._w2s(b.max_x, b.max_y, zBot, 1);
        var b3 = this._w2s(b.min_x, b.max_y, zBot, 1);

        // Determine which side faces are visible based on view angle.
        // A face is visible when its screen-space normal points towards us.
        // We use the cross product of two edge vectors in screen space.
        var sides = [
            { top: [t0, t1], bot: [b0, b1], label: "front" },  // min_y edge (front)
            { top: [t1, t2], bot: [b1, b2], label: "right" },  // max_x edge (right)
            { top: [t2, t3], bot: [b2, b3], label: "back"  },  // max_y edge (back)
            { top: [t3, t0], bot: [b3, b0], label: "left"  }   // min_x edge (left)
        ];

        // Draw bottom face first (always partially visible at angles)
        ctx.fillStyle = "rgba(0, 40, 0, 0.35)";
        ctx.beginPath();
        ctx.moveTo(b0.x, b0.y);
        ctx.lineTo(b1.x, b1.y);
        ctx.lineTo(b2.x, b2.y);
        ctx.lineTo(b3.x, b3.y);
        ctx.closePath();
        ctx.fill();

        // Draw visible side faces (darker green, slightly transparent)
        var sideFills = [
            "rgba(0, 65, 0, 0.40)",  // front
            "rgba(0, 55, 0, 0.45)",  // right (darker)
            "rgba(0, 65, 0, 0.40)",  // back
            "rgba(0, 55, 0, 0.45)"   // left (darker)
        ];

        for (var i = 0; i < 4; i++) {
            var side = sides[i];
            // Compute screen-space cross product to check face normal direction
            // Edge vectors: (top[1] - top[0]) and (bot[0] - top[0])
            var ax = side.top[1].x - side.top[0].x;
            var ay = side.top[1].y - side.top[0].y;
            var bx = side.bot[0].x - side.top[0].x;
            var by = side.bot[0].y - side.top[0].y;
            var cross = ax * by - ay * bx;

            // Positive cross = face normal points towards viewer (visible)
            if (cross > 0) {
                ctx.fillStyle = sideFills[i];
                ctx.strokeStyle = "rgba(0, 180, 80, 0.5)";
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.moveTo(side.top[0].x, side.top[0].y);
                ctx.lineTo(side.top[1].x, side.top[1].y);
                ctx.lineTo(side.bot[1].x, side.bot[1].y);
                ctx.lineTo(side.bot[0].x, side.bot[0].y);
                ctx.closePath();
                ctx.fill();
                ctx.stroke();
            }
        }

        // Draw top face last (on top of everything)
        ctx.fillStyle = "rgba(0, 80, 0, 0.25)";
        ctx.beginPath();
        ctx.moveTo(t0.x, t0.y);
        ctx.lineTo(t1.x, t1.y);
        ctx.lineTo(t2.x, t2.y);
        ctx.lineTo(t3.x, t3.y);
        ctx.closePath();
        ctx.fill();

        // Top face outline
        ctx.strokeStyle = "#00cc66";
        ctx.lineWidth = 2;
        ctx.setLineDash([6, 3]);
        ctx.stroke();
        ctx.setLineDash([]);

        // Visible side edge outlines
        ctx.strokeStyle = "#00cc66";
        ctx.lineWidth = 1;
        for (var j = 0; j < 4; j++) {
            var s = sides[j];
            var ex = s.top[1].x - s.top[0].x;
            var ey = s.top[1].y - s.top[0].y;
            var fx = s.bot[0].x - s.top[0].x;
            var fy = s.bot[0].y - s.top[0].y;
            if (ex * fy - ey * fx > 0) {
                ctx.beginPath();
                ctx.moveTo(s.top[0].x, s.top[0].y);
                ctx.lineTo(s.bot[0].x, s.bot[0].y);
                ctx.stroke();
                ctx.beginPath();
                ctx.moveTo(s.top[1].x, s.top[1].y);
                ctx.lineTo(s.bot[1].x, s.bot[1].y);
                ctx.stroke();
                ctx.beginPath();
                ctx.moveTo(s.bot[0].x, s.bot[0].y);
                ctx.lineTo(s.bot[1].x, s.bot[1].y);
                ctx.stroke();
            }
        }

    };


    // =========================================================================
    // Draw: heat map surface (colored quads between probe points)
    // =========================================================================

    WorkspaceRenderer.prototype._computeZRange = function() {
        var zMin = Infinity, zMax = -Infinity;
        var hasProbed = false;
        for (var i = 0; i < this.probePoints.length; i++) {
            if (this.probePoints[i].probed) {
                zMin = Math.min(zMin, this.probePoints[i].z);
                zMax = Math.max(zMax, this.probePoints[i].z);
                hasProbed = true;
            }
        }
        if (!hasProbed) {
            this._zMin = 0;
            this._zMax = 0;
        } else {
            this._zMin = zMin;
            this._zMax = zMax;
        }
    };

    WorkspaceRenderer.prototype._drawHeatSurface = function(ctx) {
        var points = this.probePoints;
        var gx = this.gridSize.x;
        var gy = this.gridSize.y;
        if (gx < 2 || gy < 2 || points.length < 4) return;

        // Check if any points are probed
        var hasProbed = false;
        for (var i = 0; i < points.length; i++) {
            if (points[i].probed) { hasProbed = true; break; }
        }
        if (!hasProbed) return;

        // Reconstruct grid in sorted order 
        // The probe points are in serpentine order, we need row-major sorted
        var sorted = this._getSortedGrid();
        if (!sorted) return;

        var zMin = this._zMin, zMax = this._zMax;

        // Draw filled quads between adjacent probe points
        for (var row = 0; row < gy - 1; row++) {
            for (var col = 0; col < gx - 1; col++) {
                var p00 = sorted[row * gx + col];
                var p10 = sorted[row * gx + col + 1];
                var p01 = sorted[(row + 1) * gx + col];
                var p11 = sorted[(row + 1) * gx + col + 1];

                if (!p00 || !p10 || !p01 || !p11) continue;

                // Average Z for color
                var avgZ = 0;
                var count = 0;
                if (p00.probed) { avgZ += p00.z; count++; }
                if (p10.probed) { avgZ += p10.z; count++; }
                if (p01.probed) { avgZ += p01.z; count++; }
                if (p11.probed) { avgZ += p11.z; count++; }
                if (count === 0) continue;
                avgZ /= count;

                var s00 = this._w2s(p00.x, p00.y, this.viewMode === "3d" ? (p00.probed ? p00.z : 0) : 0);
                var s10 = this._w2s(p10.x, p10.y, this.viewMode === "3d" ? (p10.probed ? p10.z : 0) : 0);
                var s01 = this._w2s(p01.x, p01.y, this.viewMode === "3d" ? (p01.probed ? p01.z : 0) : 0);
                var s11 = this._w2s(p11.x, p11.y, this.viewMode === "3d" ? (p11.probed ? p11.z : 0) : 0);

                ctx.fillStyle = heatColor(avgZ, zMin, zMax);
                ctx.beginPath();
                ctx.moveTo(s00.x, s00.y);
                ctx.lineTo(s10.x, s10.y);
                ctx.lineTo(s11.x, s11.y);
                ctx.lineTo(s01.x, s01.y);
                ctx.closePath();
                ctx.fill();

                // Quad outline
                ctx.strokeStyle = "rgba(255,255,255,0.15)";
                ctx.lineWidth = 0.5;
                ctx.stroke();
            }
        }
    };

    WorkspaceRenderer.prototype._getSortedGrid = function() {
        var points = this.probePoints;
        var gx = this.gridSize.x;
        var gy = this.gridSize.y;
        if (points.length !== gx * gy) return null;

        // Get unique sorted X and Y values
        var xSet = {}, ySet = {};
        for (var i = 0; i < points.length; i++) {
            xSet[points[i].x.toFixed(6)] = points[i].x;
            ySet[points[i].y.toFixed(6)] = points[i].y;
        }
        var xVals = Object.values(xSet).sort(function(a, b) { return a - b; });
        var yVals = Object.values(ySet).sort(function(a, b) { return a - b; });

        if (xVals.length !== gx || yVals.length !== gy) return null;

        // Build row-major sorted grid
        var lookup = {};
        for (var i = 0; i < points.length; i++) {
            var key = points[i].x.toFixed(6) + "," + points[i].y.toFixed(6);
            lookup[key] = points[i];
        }

        var sorted = [];
        for (var row = 0; row < gy; row++) {
            for (var col = 0; col < gx; col++) {
                var key = xVals[col].toFixed(6) + "," + yVals[row].toFixed(6);
                sorted.push(lookup[key] || null);
            }
        }
        return sorted;
    };

    // =========================================================================
    // Draw: probe points
    // =========================================================================

    WorkspaceRenderer.prototype._drawProbePoints = function(ctx) {
        var points = this.probePoints;
        if (points.length === 0) return;

        for (var i = 0; i < points.length; i++) {
            var p = points[i];
            var s = this._w2s(p.x, p.y, this.viewMode === "3d" && p.probed ? p.z : 0);

            var radius = Math.max(3, Math.min(6, 4 * Math.sqrt(this.zoom)));

            if (p.probed) {
                // Color by Z value
                ctx.fillStyle = heatColorSolid(p.z, this._zMin, this._zMax);
                ctx.strokeStyle = "#ffffff";
                ctx.lineWidth = 1.5;
            } else {
                // Pending
                ctx.fillStyle = "rgba(255,255,255,0.3)";
                ctx.strokeStyle = "rgba(255,255,255,0.6)";
                ctx.lineWidth = 1;
            }

            ctx.beginPath();
            ctx.arc(s.x, s.y, radius, 0, Math.PI * 2);
            ctx.fill();
            ctx.stroke();

            // Show Z value for probed points (if zoomed in enough)
            if (p.probed && this.zoom > 3) {
                ctx.fillStyle = "#ffffff";
                ctx.font = "9px monospace";
                ctx.textAlign = "center";
                ctx.fillText(p.z.toFixed(3), s.x, s.y - radius - 3);
                ctx.textAlign = "left";
            }
        }
    };

    // =========================================================================
    // Draw: toolpaths
    // =========================================================================

    WorkspaceRenderer.prototype._drawToolpaths = function(ctx) {
        for (var t = 0; t < this.toolpaths.length; t++) {
            var tp = this.toolpaths[t];
            if (!tp.points || tp.points.length < 2) continue;
            if (!tp.visible) continue;

            var baseColor = tp.color || "#ffaa00";
            ctx.lineWidth = tp.lineWidth || 1;
            ctx.globalAlpha = tp.opacity || 0.8;

            var first = this._w2s(tp.points[0].x, tp.points[0].y, tp.points[0].z || 0, 1);

            // Draw segments one at a time so each can have its own style
            for (var j = 1; j < tp.points.length; j++) {
                var pt = tp.points[j];
                var s = this._w2s(pt.x, pt.y, pt.z || 0, 1);

                if (pt.rapid) {
                    // Rapid move — dashed, faint blue
                    ctx.beginPath();
                    ctx.setLineDash([3, 3]);
                    ctx.strokeStyle = "rgba(100,100,255,0.4)";
                    ctx.lineWidth = 1;
                    ctx.moveTo(first.x, first.y);
                    ctx.lineTo(s.x, s.y);
                    ctx.stroke();
                    ctx.setLineDash([]);
                } else if (pt.spindle) {
                    // Feed move with spindle/laser ON — bright, thick
                    ctx.beginPath();
                    ctx.setLineDash([]);
                    ctx.strokeStyle = baseColor;
                    ctx.lineWidth = 2;
                    ctx.moveTo(first.x, first.y);
                    ctx.lineTo(s.x, s.y);
                    ctx.stroke();
                } else {
                    // Feed move with spindle/laser OFF — dim grey
                    ctx.beginPath();
                    ctx.setLineDash([]);
                    ctx.strokeStyle = "rgba(160,160,160,0.5)";
                    ctx.lineWidth = 1;
                    ctx.moveTo(first.x, first.y);
                    ctx.lineTo(s.x, s.y);
                    ctx.stroke();
                }
                first = s;
            }
            ctx.setLineDash([]);
            ctx.globalAlpha = 1.0;
        }
    };

    // =========================================================================
    // Draw: machine position
    // =========================================================================

    WorkspaceRenderer.prototype._drawMachinePosition = function(ctx) {
        if (!this.machinePos) return;
        var s = this._w2s(this.machinePos.x, this.machinePos.y, 0);

        // Crosshair
        var size = 12;
        ctx.strokeStyle = "#ff0000";
        ctx.lineWidth = 2;

        ctx.beginPath();
        ctx.moveTo(s.x - size, s.y);
        ctx.lineTo(s.x + size, s.y);
        ctx.moveTo(s.x, s.y - size);
        ctx.lineTo(s.x, s.y + size);
        ctx.stroke();

        // Center dot
        ctx.fillStyle = "#ff0000";
        ctx.beginPath();
        ctx.arc(s.x, s.y, 3, 0, Math.PI * 2);
        ctx.fill();

        // Position label
        ctx.fillStyle = "#ff6666";
        ctx.font = "10px monospace";
        ctx.fillText(
            "X:" + this.machinePos.x.toFixed(2) +
            " Y:" + this.machinePos.y.toFixed(2) +
            " Z:" + this.machinePos.z.toFixed(2),
            s.x + size + 4, s.y - 4
        );
    };

    // =========================================================================
    // Draw: color legend
    // =========================================================================

    WorkspaceRenderer.prototype._drawLegend = function(ctx, w, h) {
        if (this._zMin === this._zMax && this._zMin === 0) return;
        if (this.probePoints.length === 0) return;

        var lx = w - 30;
        var ly = 64;  // below the view toolbar
        var lh = 150;
        var lw = 15;

        // Title
        ctx.fillStyle = "rgba(255,255,255,0.8)";
        ctx.font = "bold 10px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(gettext("Z (mm)"), lx - lw / 2 - 5, ly - 10);

        // Gradient bar
        for (var i = 0; i < lh; i++) {
            var t = 1 - i / lh;  // top = max, bottom = min
            var z = this._zMin + t * (this._zMax - this._zMin);
            ctx.fillStyle = heatColorSolid(z, this._zMin, this._zMax);
            ctx.fillRect(lx - lw, ly + i, lw, 1);
        }

        // Border
        ctx.strokeStyle = "rgba(255,255,255,0.5)";
        ctx.lineWidth = 1;
        ctx.strokeRect(lx - lw, ly, lw, lh);

        // Min/max labels
        ctx.fillStyle = "rgba(255,255,255,0.8)";
        ctx.font = "9px monospace";
        ctx.textAlign = "right";
        ctx.fillText(this._zMax.toFixed(3), lx - lw - 4, ly + 8);
        ctx.fillText(this._zMin.toFixed(3), lx - lw - 4, ly + lh);

        // Zero line
        if (this._zMin < 0 && this._zMax > 0) {
            var zeroPos = ly + lh * (1 - (0 - this._zMin) / (this._zMax - this._zMin));
            ctx.strokeStyle = "rgba(255,255,255,0.6)";
            ctx.setLineDash([2, 2]);
            ctx.beginPath();
            ctx.moveTo(lx - lw - 3, zeroPos);
            ctx.lineTo(lx + 3, zeroPos);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.fillText("0", lx - lw - 4, zeroPos + 3);
        }

        ctx.textAlign = "left";
    };

    // =========================================================================
    // Draw: info overlay
    // =========================================================================

    WorkspaceRenderer.prototype._drawInfo = function(ctx, w, h) {
        var lines = [];

        if (this.probePoints.length > 0) {
            var probed = 0;
            for (var i = 0; i < this.probePoints.length; i++) {
                if (this.probePoints[i].probed) probed++;
            }
            lines.push(_.sprintf(gettext("Probe: %d/%d (%dx%d)"), probed, this.probePoints.length, this.gridSize.x, this.gridSize.y));
        }
        if (this._zMin !== 0 || this._zMax !== 0) {
            lines.push(_.sprintf(gettext("Z range: %s .. %s mm"), this._zMin.toFixed(3), this._zMax.toFixed(3)));
            lines.push(_.sprintf(gettext("Z span: %s mm"), (this._zMax - this._zMin).toFixed(3)));
        }
        if (this.toolpaths.length > 0) {
            var visCount = 0;
            for (var i = 0; i < this.toolpaths.length; i++) {
                if (this.toolpaths[i].visible) visCount++;
            }
            lines.push(_.sprintf(gettext("Toolpaths: %d/%d"), visCount, this.toolpaths.length));
        }

        if (lines.length === 0) return;

        ctx.fillStyle = "rgba(0,0,0,0.5)";
        var boxW = 200;
        var boxH = lines.length * 16 + 10;
        ctx.fillRect(8, h - boxH - 8, boxW, boxH);

        ctx.fillStyle = "rgba(255,255,255,0.85)";
        ctx.font = "11px monospace";
        for (var i = 0; i < lines.length; i++) {
            ctx.fillText(lines[i], 14, h - boxH + 6 + i * 16);
        }
    };

    // =========================================================================
    // Draw: cursor world coordinates
    // =========================================================================

    WorkspaceRenderer.prototype._drawCursorCoords = function(ctx, w, h) {
        if (!this._mouseWorld) return;

        var text = "X: " + this._mouseWorld.x.toFixed(2) + "  Y: " + this._mouseWorld.y.toFixed(2);

        ctx.font = "11px monospace";
        var tw = ctx.measureText(text).width;

        // Draw in top-left corner
        ctx.fillStyle = "rgba(0,0,0,0.6)";
        ctx.fillRect(8, 8, tw + 12, 20);

        ctx.fillStyle = "rgba(255,255,255,0.9)";
        ctx.fillText(text, 14, 22);
    };

    // =========================================================================
    // Public API: update data
    // =========================================================================

    WorkspaceRenderer.prototype.updateBoardBounds = function(bounds, thickness) {
        this.boardBounds = bounds;
        if (typeof thickness === "number" && thickness > 0) {
            this.boardThickness = thickness;
        }
    };

    WorkspaceRenderer.prototype.updateProbeGrid = function(points, gridX, gridY) {
        this.probePoints = points || [];
        this.gridSize = {x: gridX || 0, y: gridY || 0};
    };

    WorkspaceRenderer.prototype.updateToolpaths = function(toolpaths) {
        this.toolpaths = toolpaths || [];
    };

    WorkspaceRenderer.prototype.updateMachinePosition = function(x, y, z) {
        this.machinePos = {x: x, y: y, z: z};
        this.render();
    };

    WorkspaceRenderer.prototype.updateLayers = function(layers) {
        this.layers = layers || [];
    };

    WorkspaceRenderer.prototype.updateGerberPolygons = function(geometries, holes) {
        this.gerberPolygons = geometries || null;
        this.drillHoles = holes || [];
    };

    WorkspaceRenderer.prototype.toggleLayer = function(layer, visible) {
        switch(layer) {
            case "board": this.showBoard = visible; break;
            case "probeGrid": this.showProbeGrid = visible; break;
            case "heightmap": this.showHeatSurface = visible; break;
            case "toolpaths": this.showToolpaths = visible; break;
            case "machinePos": this.showMachinePos = visible; break;
            case "axes": this.showAxes = visible; break;
            case "layers": this.showLayers = visible; break;
            case "gerberPolygons": this.showGerberPolygons = visible; break;
        }
        this.render();
    };

    // =========================================================================
    // Export
    // =========================================================================

    global.WorkspaceRenderer = WorkspaceRenderer;

})(window);
