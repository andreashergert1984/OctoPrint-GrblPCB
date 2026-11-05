/*
 * OctoPrint-GrblPCB - Frontend JavaScript
 * 
 * View models for GRBL CNC / PCB milling control
 */

$(function() {
    // -------------------------------------------------------------------------
    // GRBL Connection Options ViewModel
    // -------------------------------------------------------------------------
    
    function GrblConnectionViewModel(parameters) {
        var self = this;

        self.settings = parameters[0];
        self.connection = parameters[1];

        // Connection options
        self.lastUpdated = ko.observable(false);
        self.portOptions = ko.observableArray([]);
        self.baudrateOptions = ko.observableArray([]);
        self.currentPort = ko.observable();
        self.currentBaudrate = ko.observable();
        
        self.validPort = ko.pureComputed(function() {
            return !self.lastUpdated() || self.portOptions().length > 0;
        });

        self.isErrorOrClosed = ko.pureComputed(function() {
            return self.connection.isErrorOrClosed();
        });

        // Called when connection data is received from server
        self.onConnectionDataReceived = function(parameters, current, last, preferred) {
            if (!parameters.grbl) return;
            
            var ports = parameters.grbl.port || [];
            var baudrates = parameters.grbl.baudrate || [115200, 250000, 57600, 38400, 19200, 9600];

            var currentPort = current.connector === "grbl" ? current.parameters?.port : undefined;
            var currentBaudrate = current.connector === "grbl" ? current.parameters?.baudrate : undefined;

            var lastPort = last.connector === "grbl" ? last.parameters?.port : undefined;
            var lastBaudrate = last.connector === "grbl" ? last.parameters?.baudrate : undefined;

            var preferredPort = preferred.connector === "grbl" ? preferred.parameters?.port : undefined;
            var preferredBaudrate = preferred.connector === "grbl" ? preferred.parameters?.baudrate : undefined;

            self.portOptions(ports);
            self.baudrateOptions(baudrates);
            self.currentPort(currentPort || lastPort || preferredPort);
            self.currentBaudrate(currentBaudrate || lastBaudrate || preferredBaudrate || 115200);
            self.lastUpdated(true);
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: GrblConnectionViewModel,
        dependencies: ["settingsViewModel", "connectionViewModel"],
        elements: ["#connection_options_grbl"]
    });

    // -------------------------------------------------------------------------
    // GRBL Control Tab ViewModel
    // -------------------------------------------------------------------------

    function GrblPcbViewModel(parameters) {
        var self = this;

        // Dependencies
        self.settings = parameters[0];
        self.loginState = parameters[1];
        self.printerState = parameters[2];
        self.connection = parameters[3];

        // Observables for GRBL state
        self.grblState = ko.observable("Unknown");
        self.isConnected = ko.observable(false);
        self.isGrblConnector = ko.observable(false);

        // Machine position
        self.machinePosX = ko.observable(0);
        self.machinePosY = ko.observable(0);
        self.machinePosZ = ko.observable(0);

        // Work position
        self.workPosX = ko.observable(0);
        self.workPosY = ko.observable(0);
        self.workPosZ = ko.observable(0);

        // Feed and spindle
        self.feedRate = ko.observable(0);
        self.spindleSpeed = ko.observable(0);
        self.spindleSpeedTarget = ko.observable(10000);
        self.machineCheckStatus = ko.observable("");
        self.machineCheckChecks = ko.observableArray([]);
        self.machineCheckLimitations = ko.observableArray([]);

        // Jog settings
        self.jogDistance = ko.observable(1);
        self.jogFeed = ko.observable(100);

        // Work Coordinate System
        self.currentWCS = ko.observable("G54");
        self.wcsOptions = ["G54", "G55", "G56", "G57", "G58", "G59"];

        // =====================================================================
        // PCB Project State
        // =====================================================================
        self.project = ko.observable(null);
        self.projectName = ko.observable(gettext("Untitled PCB"));
        self.projectLayers = ko.observableArray([]);
        self.projectDrills = ko.observableArray([]);
        self.projectOperations = ko.observableArray([]);
        self.projectState = ko.observable("");
        self.boardWidth = ko.observable(0);
        self.boardHeight = ko.observable(0);

        // Project browser
        self.savedProjects = ko.observableArray([]);
        self.showProjectBrowser = ko.observable(false);

        // File upload
        self.uploadProgress = ko.observable(0);
        self.uploadMessage = ko.observable("");

        // Autolevel
        self.autolevelGridX = ko.observable(5);
        self.autolevelGridY = ko.observable(5);
        self.autolevelMargin = ko.observable(1.0);
        self.autolevelProgress = ko.observable(0);
        self.autolevelStatus = ko.observable("");
        self.heightmapPoints = ko.observableArray([]);
        self.hasHeightmap = ko.observable(false);

        // G-code preview
        self.gcodePreview = ko.observable("");
        self.selectedOperationIndex = ko.observable(-1);

        // Inline generation parameters (override settings)
        // -- Tool --
        self.genToolDiameter = ko.observable(0.1);
        self.genToolAngle = ko.observable(60);
        // -- Isolation --
        self.genCutDepth = ko.observable(0.1);
        self.genDepthPerPass = ko.observable(0);  // 0 = full depth in one pass
        self.genPasses = ko.observable(1);
        self.genPassOverlap = ko.observable(0.0);
        self.genMaxWidth = ko.observable(0.0);  // 0 = use fixed passes
        self.genClearGaps = ko.observable(false);  // auto-detect and clear all gaps
        // -- Speeds --
        self.genFeedRate = ko.observable(500);
        self.genPlungeRate = ko.observable(100);
        self.genSpindleSpeed = ko.observable(10000);
        // -- Heights --
        self.genSafeZ = ko.observable(5.0);
        self.genTravelZ = ko.observable(2.0);
        // -- Drilling --
        self.genDrillFeed = ko.observable(100);
        self.genPeckDepth = ko.observable(0.5);
        self.genBoardThickness = ko.observable(1.6);
        self.genBreakthrough = ko.observable(0.2);
        // -- Outline --
        self.genOutlineDiameter = ko.observable(2.0);
        self.genTabWidth = ko.observable(2.0);
        self.genTabHeight = ko.observable(0.5);
        self.genNumTabs = ko.observable(4);
        // -- UV Mask Exposure --
        self.genUvOffsetX = ko.observable(0.0);
        self.genUvOffsetY = ko.observable(0.0);
        self.genUvSpotSize = ko.observable(0.5);
        self.genUvExposeSpeed = ko.observable(200);
        self.genUvExposeZ = ko.observable(5.0);
        self.genUvPower = ko.observable(255);
        self.showGenParams = ko.observable(true);

        // Transform
        self.customRotationAngle = ko.observable(45);

        // Double-sided alignment
        self.alignmentHoles = ko.observableArray([]);
        self.alignmentTransform = ko.observable(null);
        self.alignmentStatus = ko.observable("");
        self.alignmentProbing = ko.observable(false);
        self.hasAlignmentTransform = ko.computed(function() {
            return self.alignmentTransform() !== null;
        });

        // Execution state
        self.executionActive = ko.observable(false);
        self.executionProgress = ko.observable(0);
        self.executionStatus = ko.observable("");
        self.executionCurrentLine = ko.observable(0);
        self.executionTotalLines = ko.observable(0);
        self.busyGenerating = ko.observable(false);

        // Real-time override state
        self.feedOverride = ko.observable(100);
        self.rapidOverride = ko.observable(100);
        self.spindleOverride = ko.observable(100);

        // Active tab
        self.activeSubTab = ko.observable("cnc");

        // Workspace viewer
        self.workspaceViewMode = ko.observable("2d");
        self.wsShowBoard = ko.observable(true);
        self.wsShowProbeGrid = ko.observable(true);
        self.wsShowHeightmap = ko.observable(true);
        self.wsShowToolpaths = ko.observable(true);
        self.wsShowMachinePos = ko.observable(true);
        self.wsShowLayers = ko.observable(true);
        self.wsShowCopper = ko.observable(true);
        self.showProbeTable = ko.observable(false);
        self.workspaceSidebarOpen = ko.observable(true);
        self.workspaceToolpathList = ko.observableArray([]);
        self._renderer = null;
        self._rendererInitPending = false;

        // UI help mode: compact, mini, detailed
        self.uiHelpMode = ko.observable("detailed");
        self.showQuickGuide = ko.observable(true);
        self.quickGuideToggleText = ko.pureComputed(function() {
            return self.showQuickGuide() ? gettext("Hide") : gettext("Show");
        });
        self.showQuickGuideDetails = ko.observable(true);
        self.showExperiencedUsersDetails = ko.observable(true);
        self.quickGuideToggleLabel = ko.pureComputed(function() {
            return self.showQuickGuideDetails() ? gettext("Hide") : gettext("Show");
        });
        self.experiencedUsersToggleLabel = ko.pureComputed(function() {
            return self.showExperiencedUsersDetails() ? gettext("Hide") : gettext("Show");
        });

        // Per-layer visibility tracking (gerber layers)
        self._gerberLayerVisibility = {};

        // =====================================================================
        // Computed properties
        // =====================================================================

        self.navbarStateText = ko.pureComputed(function() {
            return self.isConnected() ? self.grblState() : gettext("disconnected");
        });

        self.onEventDisconnected = function() {
            self.isConnected(false);
            self.isGrblConnector(false);
            self.grblState("Unknown");
        };

        self.stateClass = ko.computed(function() {
            switch(self.grblState()) {
                case "Idle": return "text-success";
                case "Run": return "text-info";
                case "Hold": return "text-warning";
                case "Alarm": return "text-danger";
                case "Home": return "text-primary";
                default: return "text-muted";
            }
        });

        self.isIdle = ko.computed(function() {
            return self.grblState() === "Idle";
        });

        self.isAlarm = ko.computed(function() {
            return self.grblState() === "Alarm";
        });

        self.canJog = ko.computed(function() {
            var state = self.grblState();
            return self.isGrblConnector() && (state === "Idle" || state === "Jog");
        });

        self.hasProject = ko.computed(function() {
            return self.project() !== null;
        });

        self.hasCopperLayer = ko.computed(function() {
            var layers = self.projectLayers();
            return layers.some(function(l) {
                return l.layer_type === "top_copper" || l.layer_type === "bottom_copper";
            });
        });

        self.hasDrillLayer = ko.computed(function() {
            var layers = self.projectLayers();
            return layers.some(function(l) { return l.layer_type === "drill"; });
        });

        self.hasDrillHoles = ko.computed(function() {
            return self.projectDrills().length > 0;
        });

        self.drillHoleCount = ko.computed(function() {
            var total = 0;
            self.projectDrills().forEach(function(d) { total += (d.count || 0); });
            return total;
        });

        self.drillHoleCountText = ko.pureComputed(function() {
            return _.sprintf(gettext("(%(count)d holes)"), {count: self.drillHoleCount()});
        });

        self.drillButtonTitle = ko.pureComputed(function() {
            return self.hasDrillLayer() && !self.hasDrillHoles() ? gettext("Drill file contains no holes") : "";
        });

        self.uploadMessageIsError = ko.pureComputed(function() {
            return self.uploadMessage().indexOf(gettext("Error")) === 0;
        });

        self.projectSummaryText = function(project) {
            return _.sprintf(gettext("%(layers)d layers, %(operations)d operations"), {
                layers: project.layers.length,
                operations: project.operations_count
            });
        };

        self.hasOutlineLayer = ko.computed(function() {
            var layers = self.projectLayers();
            return layers.some(function(l) { return l.layer_type === "outline"; });
        });

        self.hasMaskLayer = ko.computed(function() {
            var layers = self.projectLayers();
            return layers.some(function(l) {
                return l.layer_type === "top_mask" || l.layer_type === "bottom_mask";
            });
        });

        self.canGenerateIsolation = ko.computed(function() {
            return self.hasProject() && self.hasCopperLayer();
        });

        self.canSetupGrid = ko.computed(function() {
            return self.hasProject() && self.boardWidth() > 0;
        });

        self.canAutolevel = ko.computed(function() {
            return self.hasProject() && self.isGrblConnector() && self.boardWidth() > 0;
        });

        self.showHelpNotes = ko.pureComputed(function() {
            return self.uiHelpMode() === "detailed";
        });

        self.showDetailedHelp = ko.pureComputed(function() {
            return self.uiHelpMode() === "detailed";
        });

        self.showMiniGuide = ko.pureComputed(function() {
            return self.uiHelpMode() === "mini";
        });

        // Helper for gerber layer checkbox binding
        self.wsLayerVisible = function(layerType) {
            if (!(layerType in self._gerberLayerVisibility)) {
                self._gerberLayerVisibility[layerType] = ko.observable(true);
            }
            return self._gerberLayerVisibility[layerType];
        };

        self.toggleGerberLayer = function(layerType) {
            var obs = self.wsLayerVisible(layerType);
            obs(!obs());
            self._syncLayersToRenderer();
        };

        // =====================================================================
        // CNC Control Commands
        // =====================================================================

        self._sendCommand = function(command, data) {
            data = data || {};
            return OctoPrint.simpleApiCommand("grblpcb", command, data)
                .fail(function(xhr) {
                    var msg = xhr.responseJSON ? xhr.responseJSON.error : xhr.statusText;
                    console.error("GrblPCB command '" + command + "' failed:", msg);
                });
        };

        self._applyMachineCheckReport = function(report) {
            report = report || {};

            self.machineCheckStatus(report.summary || "");
            self.machineCheckChecks(report.checks || []);
            self.machineCheckLimitations(report.limitations || []);

            if (report.snapshot) {
                var snapshot = report.snapshot;
                if (snapshot.state) {
                    self.grblState(snapshot.state);
                }
                if (snapshot.connected !== undefined) {
                    self.isConnected(!!snapshot.connected);
                    self.isGrblConnector(!!snapshot.connected);
                }
                if (snapshot.machine_pos) {
                    self.machinePosX(snapshot.machine_pos.x.toFixed(3));
                    self.machinePosY(snapshot.machine_pos.y.toFixed(3));
                    self.machinePosZ(snapshot.machine_pos.z.toFixed(3));
                }
                if (snapshot.work_pos) {
                    self.workPosX(snapshot.work_pos.x.toFixed(3));
                    self.workPosY(snapshot.work_pos.y.toFixed(3));
                    self.workPosZ(snapshot.work_pos.z.toFixed(3));
                }
                if (snapshot.feed !== undefined) {
                    self.feedRate(snapshot.feed);
                }
                if (snapshot.spindle !== undefined) {
                    self.spindleSpeed(snapshot.spindle);
                }
                if (snapshot.wcs) {
                    self.currentWCS(snapshot.wcs);
                }
            }
        };

        self.homeAll = function() { self._sendCommand("home"); };
        self.unlock = function() { self._sendCommand("unlock"); };
        self.softReset = function() { self._sendCommand("reset"); };
        self.feedHold = function() { self._sendCommand("hold"); };
        self.resume = function() { self._sendCommand("resume"); };
        self.requestStatus = function() {
            self._sendCommand("status").done(function(response) {
                if (response && response.report) {
                    self._applyMachineCheckReport(response.report);
                }
            });
        };

        self.runMachineCheck = function() {
            self._sendCommand("machine_check").done(function(response) {
                if (response && response.report) {
                    self._applyMachineCheckReport(response.report);
                }
            });
        };

        self.probeZ = function() { self._sendCommand("probe", {axis: "Z"}); };
        self.probeX = function() { self._sendCommand("probe", {axis: "X"}); };
        self.probeY = function() { self._sendCommand("probe", {axis: "Y"}); };

        self.zeroX = function() { self._sendCommand("zero_axis", {axis: "X"}); };
        self.zeroY = function() { self._sendCommand("zero_axis", {axis: "Y"}); };
        self.zeroZ = function() { self._sendCommand("zero_axis", {axis: "Z"}); };
        self.zeroAll = function() { self._sendCommand("zero_axis", {axis: "all"}); };
        self.gotoZero = function() { self._sendCommand("goto_zero"); };

        self.setWCS = function(wcs) {
            self.currentWCS(wcs);
            self._sendCommand("set_wcs", {wcs: wcs});
        };

        self.spindleOn = function() { self._sendCommand("spindle_on"); };
        self.spindleOff = function() { self._sendCommand("spindle_off"); };
        self.setSpindleSpeed = function() {
            self._sendCommand("set_spindle", {speed: self.spindleSpeedTarget()});
        };

        self.jog = function(axis, direction) {
            var distance = self.jogDistance() * direction;
            self._sendCommand("jog", {
                axis: axis,
                distance: distance,
                feed: self.jogFeed()
            });
        };

        self.jogXPlus = function() { self.jog("X", 1); };
        self.jogXMinus = function() { self.jog("X", -1); };
        self.jogYPlus = function() { self.jog("Y", 1); };
        self.jogYMinus = function() { self.jog("Y", -1); };
        self.jogZPlus = function() { self.jog("Z", 1); };
        self.jogZMinus = function() { self.jog("Z", -1); };

        self.setJogDistance = function(distance) {
            self.jogDistance(distance);
        };

        // =====================================================================
        // PCB Project Commands
        // =====================================================================

        self.newProject = function() {
            OctoPrint.simpleApiCommand("grblpcb", "new_project", {name: self.projectName()})
                .done(function(response) {
                    if (response.project) {
                        self._updateProjectState(response.project);
                        self.uploadMessage(_.sprintf(gettext("Project '%s' created"), response.project.name));
                        self.refreshProjectList();
                    }
                })
                .fail(function(xhr) {
                    self.uploadMessage(gettext("Error creating project: ") + (xhr.responseJSON?.error || gettext("unknown")));
                });
        };

        // Project Browser
        self.refreshProjectList = function() {
            OctoPrint.simpleApiCommand("grblpcb", "list_projects", {})
                .done(function(response) {
                    self.savedProjects(response.projects || []);
                });
        };

        self.toggleProjectBrowser = function() {
            var show = !self.showProjectBrowser();
            self.showProjectBrowser(show);
            if (show) {
                self.refreshProjectList();
            }
        };

        self.loadProject = function(projectSummary) {
            OctoPrint.simpleApiCommand("grblpcb", "load_project", {project_id: projectSummary.id})
                .done(function(response) {
                    if (response.project) {
                        self._updateProjectState(response.project);
                        self.projectName(response.project.name);
                        self.uploadMessage(_.sprintf(gettext("Project '%s' loaded"), response.project.name));
                        self.showProjectBrowser(false);
                    }
                })
                .fail(function(xhr) {
                    self.uploadMessage(gettext("Error loading project: ") + (xhr.responseJSON?.error || gettext("unknown")));
                });
        };

        self.deleteProject = function(projectSummary) {
            if (!confirm(_.sprintf(gettext("Delete project '%s'? This cannot be undone."), projectSummary.name))) {
                return;
            }
            OctoPrint.simpleApiCommand("grblpcb", "delete_project", {project_id: projectSummary.id})
                .done(function(response) {
                    self.savedProjects(response.projects || []);
                    // If we deleted the active project, clear the UI
                    if (self.project() && self.project().id === projectSummary.id) {
                        self.project(null);
                        self.projectLayers([]);
                        self.projectOperations([]);
                        self.projectState("");
                        self.boardWidth(0);
                        self.boardHeight(0);
                        self._syncWorkspaceData();
                    }
                    self.uploadMessage(_.sprintf(gettext("Project '%s' deleted"), projectSummary.name));
                })
                .fail(function(xhr) {
                    self.uploadMessage(gettext("Error deleting project: ") + (xhr.responseJSON?.error || gettext("unknown")));
                });
        };

        self._updateProjectState = function(projectData) {
            if (!projectData) return;
            self.project(projectData);
            self.projectState(projectData.state);
            self.boardWidth(projectData.board_width);
            self.boardHeight(projectData.board_height);

            // Update layers list
            var layers = [];
            for (var lt in projectData.layers) {
                var l = projectData.layers[lt];
                l.layer_type = lt;
                layers.push(l);
            }
            self.projectLayers(layers);

            // Update operations
            if (projectData.operations) {
                self.projectOperations(projectData.operations);
            }

            // Update drills
            self.projectDrills(projectData.drills || []);

            self.hasHeightmap(projectData.has_height_map || false);

            // Restore heightmap probe points if available
            if (projectData.height_map && projectData.height_map.points) {
                self.heightmapPoints(projectData.height_map.points);
                self.hasHeightmap(true);
            }

            // Restore alignment data
            self.alignmentHoles(projectData.alignment_holes || []);
            self.alignmentTransform(projectData.alignment_transform || null);

            // Sync workspace with new project data
            self._syncWorkspaceData();

            // Load Gerber polygon geometry for canvas
            self._loadGerberGeometry();

            // Auto-load toolpaths if operations exist
            if (projectData.operations && projectData.operations.length > 0) {
                self.loadToolpathsIntoWorkspace();
            }
        };

        // File Upload
        self.uploadFiles = function(_, event) {
            var files = event.target.files;
            if (!files || files.length === 0) return;

            // Ensure project exists
            if (!self.hasProject()) {
                OctoPrint.simpleApiCommand("grblpcb", "new_project", {name: self.projectName()})
                    .done(function(response) {
                        self._updateProjectState(response.project);
                        self._doUpload(files);
                    });
            } else {
                self._doUpload(files);
            }
        };

        self._doUpload = function(files) {
            var formData = new FormData();
            for (var i = 0; i < files.length; i++) {
                formData.append("file_" + i, files[i]);
            }

            self.uploadMessage(_.sprintf(gettext("Uploading %d file(s)..."), files.length));
            self.uploadProgress(10);

            $.ajax({
                url: OctoPrint.getBlueprintUrl("grblpcb") + "upload",
                type: "POST",
                data: formData,
                processData: false,
                contentType: false,
                headers: {
                    "X-CSRF-Token": OctoPrint.getCookie("csrf_token")
                },
                success: function(response) {
                    self.uploadProgress(100);
                    self.uploadMessage(
                        _.sprintf(gettext("Uploaded %d file(s). Board: %s x %s mm"),
                            response.files.length,
                            (response.project.board_width || 0).toFixed(1),
                            (response.project.board_height || 0).toFixed(1))
                    );
                    self._updateProjectState(response.project);
                    // Reset file input so the same file can be re-uploaded
                    $("#grblpcb_file_upload").val("");
                },
                error: function(xhr) {
                    self.uploadProgress(0);
                    self.uploadMessage(gettext("Upload failed: ") + (xhr.responseJSON?.error || gettext("unknown error")));
                }
            });
        };

        self.importLayer = function(filename, layerType) {
            self._sendCommand("import_layer", {
                filename: filename,
                layer_type: layerType
            });
        };

        self.removeLayer = function(layerType) {
            OctoPrint.simpleApiCommand("grblpcb", "remove_layer", {layer_type: layerType})
                .done(function(response) {
                    self._updateProjectState(response.project);
                });
        };

        self.rotateProject = function(angle) {
            if (self.busyGenerating()) return;
            if (!angle || isNaN(angle)) return;
            self.busyGenerating(true);
            self.uploadMessage(_.sprintf(gettext("Rotating board by %d°..."), angle));
            OctoPrint.simpleApiCommand("grblpcb", "rotate_project", {angle: angle})
                .done(function(response) {
                    self._updateProjectState(response.project);
                    self.uploadMessage(
                        _.sprintf(gettext("Rotated %d°, board: %s × %s mm"),
                            angle,
                            response.board_width.toFixed(1),
                            response.board_height.toFixed(1))
                    );
                    self._syncWorkspaceData();
                    if (self._renderer) self._renderer.fitToView();
                })
                .fail(function(xhr) {
                    self.uploadMessage(gettext("Rotation error: ") + (xhr.responseJSON?.error || gettext("failed")));
                })
                .always(function() {
                    self.busyGenerating(false);
                });
        };

        // =====================================================================
        // Toolpath Generation
        // =====================================================================

        self.generateIsolation = function(layerType) {
            if (self.busyGenerating()) return;
            layerType = layerType || "top_copper";
            self.busyGenerating(true);
            self.uploadMessage(gettext("Generating isolation toolpaths..."));
            OctoPrint.simpleApiCommand("grblpcb", "generate_isolation", {
                layer_type: layerType,
                tool_diameter: parseFloat(self.genToolDiameter()),
                tool_angle: parseFloat(self.genToolAngle()),
                cut_depth: parseFloat(self.genCutDepth()),
                depth_per_pass: parseFloat(self.genDepthPerPass()),
                feed_rate: parseFloat(self.genFeedRate()),
                plunge_rate: parseFloat(self.genPlungeRate()),
                passes: parseInt(self.genPasses()),
                pass_overlap: parseFloat(self.genPassOverlap()),
                max_width: parseFloat(self.genMaxWidth()),
                clear_gaps: self.genClearGaps() ? true : false,
                spindle_speed: parseInt(self.genSpindleSpeed()),
                safe_z: parseFloat(self.genSafeZ()),
                travel_z: parseFloat(self.genTravelZ())
            }).done(function(response) {
                self.uploadMessage(
                    _.sprintf(gettext("Isolation G-code generated: %d lines"), response.gcode_lines)
                );
                self._refreshProject();
            }).fail(function(xhr) {
                self.uploadMessage(gettext("Error: ") + (xhr.responseJSON?.error || gettext("failed")));
            }).always(function() {
                self.busyGenerating(false);
            });
        };

        self.generateDrill = function() {
            if (self.busyGenerating()) return;
            self.busyGenerating(true);
            self.uploadMessage(gettext("Generating drill G-code..."));
            OctoPrint.simpleApiCommand("grblpcb", "generate_drill", {
                drill_feed: parseFloat(self.genDrillFeed()),
                peck_depth: parseFloat(self.genPeckDepth()),
                spindle_speed: parseInt(self.genSpindleSpeed()),
                safe_z: parseFloat(self.genSafeZ()),
                travel_z: parseFloat(self.genTravelZ()),
                board_thickness: parseFloat(self.genBoardThickness()),
                breakthrough_depth: parseFloat(self.genBreakthrough())
            })
                .done(function(response) {
                    self.uploadMessage(
                        _.sprintf(gettext("Drill G-code generated: %d lines"), response.gcode_lines)
                    );
                    self._refreshProject();
                }).fail(function(xhr) {
                    self.uploadMessage(gettext("Error: ") + (xhr.responseJSON?.error || gettext("failed")));
                }).always(function() {
                    self.busyGenerating(false);
                });
        };

        self.generateOutline = function() {
            if (self.busyGenerating()) return;
            self.busyGenerating(true);
            self.uploadMessage(gettext("Generating outline G-code..."));
            OctoPrint.simpleApiCommand("grblpcb", "generate_outline", {
                tool_diameter: parseFloat(self.genOutlineDiameter()),
                feed_rate: parseFloat(self.genFeedRate()),
                plunge_rate: parseFloat(self.genPlungeRate()),
                spindle_speed: parseInt(self.genSpindleSpeed()),
                safe_z: parseFloat(self.genSafeZ()),
                travel_z: parseFloat(self.genTravelZ()),
                board_thickness: parseFloat(self.genBoardThickness()),
                breakthrough_depth: parseFloat(self.genBreakthrough()),
                tab_width: parseFloat(self.genTabWidth()),
                tab_height: parseFloat(self.genTabHeight()),
                num_tabs: parseInt(self.genNumTabs())
            })
                .done(function(response) {
                    self.uploadMessage(
                        _.sprintf(gettext("Outline G-code generated: %d lines"), response.gcode_lines)
                    );
                    self._refreshProject();
                }).fail(function(xhr) {
                    self.uploadMessage(gettext("Error: ") + (xhr.responseJSON?.error || gettext("failed")));
                }).always(function() {
                    self.busyGenerating(false);
                });
        };

        self.generateMaskExpose = function(layerType) {
            if (self.busyGenerating()) return;
            self.busyGenerating(true);
            self.uploadMessage(gettext("Generating mask exposure G-code..."));
            OctoPrint.simpleApiCommand("grblpcb", "generate_mask_expose", {
                layer_type: layerType || "top_mask",
                uv_offset_x: parseFloat(self.genUvOffsetX()),
                uv_offset_y: parseFloat(self.genUvOffsetY()),
                uv_spot_size: parseFloat(self.genUvSpotSize()),
                uv_expose_speed: parseFloat(self.genUvExposeSpeed()),
                uv_expose_z: parseFloat(self.genUvExposeZ()),
                uv_power: parseInt(self.genUvPower()),
                safe_z: parseFloat(self.genSafeZ())
            })
                .done(function(response) {
                    self.uploadMessage(
                        _.sprintf(gettext("Mask exposure G-code generated: %d lines"), response.gcode_lines)
                    );
                    self._refreshProject();
                }).fail(function(xhr) {
                    self.uploadMessage(gettext("Error: ") + (xhr.responseJSON?.error || gettext("failed")));
                }).always(function() {
                    self.busyGenerating(false);
                });
        };

        self._refreshProject = function() {
            OctoPrint.simpleApiCommand("grblpcb", "get_project", {})
                .done(function(response) {
                    if (response.project) {
                        self._updateProjectState(response.project);
                        self.projectName(response.project.name);
                    }
                });
        };

        // =====================================================================
        // Autolevel
        // =====================================================================

        self.setupAutolevel = function() {
            OctoPrint.simpleApiCommand("grblpcb", "setup_autolevel", {
                grid_x: parseInt(self.autolevelGridX()),
                grid_y: parseInt(self.autolevelGridY()),
                margin: parseFloat(self.autolevelMargin())
            }).done(function(response) {
                self.heightmapPoints(response.heightmap.points);
                self.hasHeightmap(true);
                self.autolevelStatus(
                    _.sprintf(gettext("Grid ready: %d probe points"), response.total_points)
                );
                self._syncWorkspaceData();
                if (self._renderer) self._renderer.fitToView();
            });
        };

        self.startAutolevel = function() {
            self.autolevelProgress(0);
            self.autolevelStatus(gettext("Probing started..."));
            self._sendCommand("start_autolevel");
        };

        self.stopAutolevel = function() {
            self._sendCommand("stop_autolevel");
            self.autolevelStatus(gettext("Probing stopped"));
        };

        self.applyAutolevel = function(operationIndex) {
            OctoPrint.simpleApiCommand("grblpcb", "apply_autolevel", {
                operation_index: operationIndex
            }).done(function(response) {
                self.uploadMessage(_.sprintf(gettext("Autolevel applied: %d lines"), response.gcode_lines));
                self._refreshProject();
            }).fail(function(xhr) {
                self.uploadMessage(gettext("Error: ") + (xhr.responseJSON?.error || gettext("failed")));
            });
        };

        // =====================================================================
        // Workspace Viewer
        // =====================================================================

        self._initWorkspace = function() {
            if (self._renderer) return;
            var canvas = document.getElementById("grblpcb-workspace-canvas");
            if (!canvas) {
                // Canvas not in DOM yet, schedule retry
                self._rendererInitPending = true;
                return;
            }
            self._renderer = new WorkspaceRenderer("grblpcb-workspace-canvas");
            self._rendererInitPending = false;
            self._syncWorkspaceData();
            self._loadGerberGeometry();
            // Load toolpaths if operations exist (e.g. new browser tab)
            var ops = self.projectOperations();
            if (ops && ops.length > 0) {
                self.loadToolpathsIntoWorkspace();
            }
            self._renderer.fitToView();
        };

        self._syncWorkspaceData = function() {
            if (!self._renderer) return;

            // Board bounds — CNC coordinates (0,0 at board corner)
            if (self.boardWidth() > 0) {
                self._renderer.updateBoardBounds({
                    min_x: 0,
                    min_y: 0,
                    max_x: self.boardWidth(),
                    max_y: self.boardHeight()
                }, parseFloat(self.genBoardThickness()) || 1.6);
            } else {
                self._renderer.updateBoardBounds(null);
            }

            // Probe grid
            self._renderer.updateProbeGrid(
                self.heightmapPoints(),
                parseInt(self.autolevelGridX()),
                parseInt(self.autolevelGridY())
            );

            // Gerber layers
            self._syncLayersToRenderer();

            self._renderer.render();
        };

        self._syncLayersToRenderer = function() {
            if (!self._renderer) return;
            var proj = self.project();
            var ox = proj ? (proj.board_min_x || 0) : 0;
            var oy = proj ? (proj.board_min_y || 0) : 0;
            var layers = self.projectLayers();
            var rendererLayers = [];
            for (var i = 0; i < layers.length; i++) {
                var l = layers[i];
                var vis = self.wsLayerVisible(l.layer_type);
                rendererLayers.push({
                    layer_type: l.layer_type,
                    min_x: (l.min_x || 0) - ox,
                    min_y: (l.min_y || 0) - oy,
                    max_x: (l.max_x || 0) - ox,
                    max_y: (l.max_y || 0) - oy,
                    width: l.width,
                    height: l.height,
                    visible: vis()
                });
            }
            self._renderer.updateLayers(rendererLayers);
            self._renderer.render();
        };

        self._loadGerberGeometry = function() {
            if (!self._renderer) return;
            if (!self.hasProject()) return;
            OctoPrint.simpleApiCommand("grblpcb", "get_layer_geometry", {})
                .done(function(response) {
                    if (response.geometries && self._renderer) {
                        self._renderer.updateGerberPolygons(response.geometries);
                        self._renderer.render();
                    }
                })
                .fail(function() {
                    console.warn("GrblPCB: failed to load Gerber geometry for canvas");
                });
        };

        self._scheduleRender = function() {
            if (!self._renderer) return;
            self._renderer.render();
        };

        self.setWorkspaceView = function(mode) {
            self.workspaceViewMode(mode);
            if (self._renderer) {
                self._renderer.setViewMode(mode);
            }
        };

        self.fitWorkspaceView = function() {
            if (self._renderer) {
                self._renderer.fitToView();
            }
        };

        self.toggleProbeTable = function() {
            self.showProbeTable(!self.showProbeTable());
        };

        self.toggleWorkspaceSidebar = function() {
            self.workspaceSidebarOpen(!self.workspaceSidebarOpen());
        };

        self.toggleWorkspaceToolpath = function(index) {
            // No manual toggle needed, ko checked binding handles it
            self._updateRendererToolpaths();
        };

        self._updateRendererToolpaths = function() {
            if (!self._renderer) return;
            var tpList = self.workspaceToolpathList();
            var rendererTps = [];
            for (var i = 0; i < tpList.length; i++) {
                rendererTps.push({
                    points: tpList[i].points,
                    color: tpList[i].color,
                    visible: tpList[i].visible(),
                    lineWidth: 1,
                    opacity: 0.8
                });
            }
            self._renderer.updateToolpaths(rendererTps);
            self._renderer.render();
        };

        self.loadToolpathsIntoWorkspace = function() {
            // Request parsed G-code paths from the server
            OctoPrint.simpleApiCommand("grblpcb", "get_toolpath_preview", {})
                .done(function(response) {
                    if (!response.toolpaths) return;
                    var colors = ["#ffaa00", "#00ccff", "#ff66cc", "#66ff66", "#ff6644", "#aaaaff"];
                    var list = [];
                    for (var i = 0; i < response.toolpaths.length; i++) {
                        var tp = response.toolpaths[i];
                        var vis = ko.observable(true);
                        vis.subscribe(function() { self._updateRendererToolpaths(); });
                        list.push({
                            name: GrblPcbLabels.operation(tp),
                            color: colors[i % colors.length],
                            points: tp.points,
                            visible: vis
                        });
                    }
                    self.workspaceToolpathList(list);
                    self._updateRendererToolpaths();
                });
        };

        // Subscribe to layer toggle changes
        self.wsShowBoard.subscribe(function(v) {
            if (self._renderer) { self._renderer.toggleLayer("board", v); }
        });
        self.wsShowProbeGrid.subscribe(function(v) {
            if (self._renderer) { self._renderer.toggleLayer("probeGrid", v); }
        });
        self.wsShowHeightmap.subscribe(function(v) {
            if (self._renderer) { self._renderer.toggleLayer("heightmap", v); }
        });
        self.wsShowToolpaths.subscribe(function(v) {
            if (self._renderer) { self._renderer.toggleLayer("toolpaths", v); }
        });
        self.wsShowMachinePos.subscribe(function(v) {
            if (self._renderer) { self._renderer.toggleLayer("machinePos", v); }
        });
        self.wsShowLayers.subscribe(function(v) {
            if (self._renderer) { self._renderer.toggleLayer("layers", v); }
        });
        self.wsShowCopper.subscribe(function(v) {
            if (self._renderer) { self._renderer.toggleLayer("gerberPolygons", v); }
        });

        // =====================================================================
        // Operations
        // =====================================================================

        self.downloadGcode = function(operationIndex) {
            OctoPrint.simpleApiCommand("grblpcb", "download_gcode", {
                operation_index: operationIndex
            }).done(function(response) {
                // Trigger download
                var blob = new Blob([response.gcode], {type: "text/plain"});
                var url = URL.createObjectURL(blob);
                var a = document.createElement("a");
                a.href = url;
                a.download = response.filename;
                a.click();
                URL.revokeObjectURL(url);
            });
        };

        self.mirrorGcode = function(operationIndex) {
            OctoPrint.simpleApiCommand("grblpcb", "mirror_gcode", {
                operation_index: operationIndex
            }).done(function(response) {
                self.uploadMessage(gettext("Mirrored G-code created"));
                self._refreshProject();
            });
        };

        // =================================================================
        // Double-sided alignment
        // =================================================================

        self.setAlignmentHolesFromDrills = function() {
            // Let user pick alignment holes from the drill list.
            // Use the 2 holes that are farthest apart (corner-to-corner).
            var drills = self.projectDrills();
            var allHoles = [];
            for (var d = 0; d < drills.length; d++) {
                var drill = drills[d];
                for (var h = 0; h < drill.holes.length; h++) {
                    allHoles.push({
                        x: drill.holes[h][0],
                        y: drill.holes[h][1],
                        diameter: drill.diameter
                    });
                }
            }
            if (allHoles.length < 2) {
                self.alignmentStatus(gettext("Need at least 2 drill holes"));
                return;
            }

            // Find the 2 holes with maximum distance (best alignment baseline)
            var bestDist = 0, bestI = 0, bestJ = 1;
            for (var i = 0; i < allHoles.length; i++) {
                for (var j = i + 1; j < allHoles.length; j++) {
                    var dx = allHoles[i].x - allHoles[j].x;
                    var dy = allHoles[i].y - allHoles[j].y;
                    var dist = dx * dx + dy * dy;
                    if (dist > bestDist) {
                        bestDist = dist;
                        bestI = i;
                        bestJ = j;
                    }
                }
            }

            var selected = [
                {expected_x: allHoles[bestI].x, expected_y: allHoles[bestI].y, diameter: allHoles[bestI].diameter},
                {expected_x: allHoles[bestJ].x, expected_y: allHoles[bestJ].y, diameter: allHoles[bestJ].diameter}
            ];

            OctoPrint.simpleApiCommand("grblpcb", "set_alignment_holes", {
                holes: selected
            }).done(function(response) {
                self.alignmentHoles(response.alignment_holes);
                self.alignmentTransform(null);
                self.alignmentStatus(
                    _.sprintf(gettext("Set %d alignment holes (dist=%smm)"), selected.length, Math.sqrt(bestDist).toFixed(1))
                );
            });
        };

        self.setAlignmentHoleManual = function(holes) {
            // Allow setting specific holes: [{expected_x, expected_y, diameter}, ...]
            OctoPrint.simpleApiCommand("grblpcb", "set_alignment_holes", {
                holes: holes
            }).done(function(response) {
                self.alignmentHoles(response.alignment_holes);
                self.alignmentTransform(null);
                self.alignmentStatus(_.sprintf(gettext("Set %d alignment holes"), holes.length));
            });
        };

        self.probeAlignment = function() {
            if (self.alignmentHoles().length < 2) {
                self.alignmentStatus(gettext("Set alignment holes first"));
                return;
            }
            self.alignmentProbing(true);
            self.alignmentStatus(gettext("Probing alignment holes..."));
            self._sendCommand("probe_alignment").fail(function() {
                self.alignmentProbing(false);
                self.alignmentStatus(gettext("Failed to start alignment probing"));
            });
        };

        self.stopAlignmentProbe = function() {
            self._sendCommand("stop_alignment_probe");
            self.alignmentProbing(false);
            self.alignmentStatus(gettext("Probing stopped"));
        };

        self.computeAlignment = function() {
            OctoPrint.simpleApiCommand("grblpcb", "compute_alignment", {})
                .done(function(response) {
                    self.alignmentTransform(response.transform);
                    self.alignmentStatus(response.message);
                })
                .fail(function(xhr) {
                    var msg = xhr.responseJSON ? xhr.responseJSON.error : gettext("Computation failed");
                    self.alignmentStatus(gettext("Error: ") + msg);
                });
        };

        self.applyAlignment = function(operationIndex) {
            OctoPrint.simpleApiCommand("grblpcb", "apply_alignment", {
                operation_index: operationIndex
            }).done(function(response) {
                self.uploadMessage(response.message);
                self._refreshProject();
            });
        };

        self.applyAlignmentToAll = function() {
            // Apply alignment transform to all mirrored operations
            var ops = self.projectOperations();
            var applied = 0;
            for (var i = 0; i < ops.length; i++) {
                if (ops[i].name && ops[i].name.indexOf("(mirrored)") !== -1 && ops[i].has_gcode) {
                    self.applyAlignment(i);
                    applied++;
                }
            }
            if (applied === 0) {
                self.alignmentStatus(gettext("No mirrored operations found to align"));
            }
        };

        self.clearAlignment = function() {
            self._sendCommand("clear_alignment").done(function() {
                self.alignmentHoles([]);
                self.alignmentTransform(null);
                self.alignmentStatus(gettext("Alignment cleared"));
            });
        };

        self.selectOperation = function(index) {
            self.selectedOperationIndex(index);
        };

        self.deleteOperation = function(index) {
            if (!confirm(gettext("Delete this operation?"))) return;
            OctoPrint.simpleApiCommand("grblpcb", "delete_operation", {
                operation_index: index
            }).done(function(response) {
                if (response.project) {
                    self._updateProjectState(response.project);
                    self.uploadMessage(gettext("Operation deleted"));
                }
            });
        };

        self.moveOperationUp = function(index) {
            if (index <= 0) return;
            var ops = self.projectOperations();
            var order = [];
            for (var i = 0; i < ops.length; i++) order.push(i);
            // Swap index with index-1
            order[index] = index - 1;
            order[index - 1] = index;
            OctoPrint.simpleApiCommand("grblpcb", "reorder_operations", {
                new_order: order
            }).done(function(response) {
                if (response.project) {
                    self._updateProjectState(response.project);
                }
            });
        };

        self.moveOperationDown = function(index) {
            var ops = self.projectOperations();
            if (index >= ops.length - 1) return;
            var order = [];
            for (var i = 0; i < ops.length; i++) order.push(i);
            order[index] = index + 1;
            order[index + 1] = index;
            OctoPrint.simpleApiCommand("grblpcb", "reorder_operations", {
                new_order: order
            }).done(function(response) {
                if (response.project) {
                    self._updateProjectState(response.project);
                }
            });
        };

        // =====================================================================
        // G-code Execution
        // =====================================================================

        self.sendToCnc = function(operationIndex, force) {
            if (!force && !confirm(gettext("Send this operation to the CNC machine for execution?"))) return;
            self.executionOperationLabel = GrblPcbLabels.operation(self.projectOperations()[operationIndex]);
            self.executionActive(true);
            self.executionProgress(0);
            self.executionStatus(gettext("Starting..."));
            OctoPrint.simpleApiCommand("grblpcb", "send_to_cnc", {
                operation_index: operationIndex,
                force: !!force
            }).done(function(response) {
                if (response.result === "warnings") {
                    // Pre-flight warnings: show them and ask user to confirm
                    self.executionActive(false);
                    var msg = _.map(response.warnings, self._preflightWarningText).join("\n\n");
                    if (confirm(gettext("Pre-flight warnings:") + "\n\n" + msg + "\n\n" + gettext("Proceed anyway?"))) {
                        self.sendToCnc(operationIndex, true);
                    } else {
                        self.executionStatus(gettext("Aborted: check Z-zero and autolevel"));
                    }
                    return;
                }
                self.executionStatus(_.sprintf(gettext("Streaming: %s"), self.executionOperationLabel));
                self.executionTotalLines(response.total_lines || 0);
            }).fail(function(xhr) {
                self.executionActive(false);
                self.executionStatus(gettext("Error: ") + (xhr.responseJSON?.error || gettext("failed")));
            });
        };

        self._preflightWarningText = function(warning) {
            switch (warning.code) {
                case "z_not_zeroed":
                    return gettext("Z-zero has not been set since connecting. Probe Z and zero it at the board surface before milling (Probe Z, then Zero Z).");
                case "heightmap_origin_moved":
                    return _.sprintf(gettext("The work zero has moved by X %s mm, Y %s mm since the height map was probed. The leveled G-code would correct the wrong spots. Probe the board again."), warning.dx, warning.dy);
                case "heightmap_origin_unknown":
                    return gettext("It is unknown where the work zero was when the height map was probed. Probe the board again so the leveled G-code matches the clamped board.");
                default:
                    return warning.code;
            }
        };

        self.stopExecution = function() {
            OctoPrint.simpleApiCommand("grblpcb", "stop_execution", {})
                .done(function() {
                    self.executionActive(false);
                    self.executionStatus(gettext("Stopped"));
                });
        };

        self.confirmToolChange = function() {
            OctoPrint.simpleApiCommand("grblpcb", "confirm_tool_change", {})
                .done(function() {
                    self.executionStatus(gettext("Resuming after tool change..."));
                });
        };

        // =====================================================================
        // Real-time Overrides
        // =====================================================================

        self.sendOverride = function(overrideType, action) {
            OctoPrint.simpleApiCommand("grblpcb", "send_override", {
                override_type: overrideType,
                action: action
            });
        };

        self.feedPlus10 = function() { self.sendOverride("feed", "plus10"); };
        self.feedMinus10 = function() { self.sendOverride("feed", "minus10"); };
        self.feedPlus1 = function() { self.sendOverride("feed", "plus1"); };
        self.feedMinus1 = function() { self.sendOverride("feed", "minus1"); };
        self.feedReset = function() { self.sendOverride("feed", "reset"); };

        self.rapidFull = function() { self.sendOverride("rapid", "100"); };
        self.rapidHalf = function() { self.sendOverride("rapid", "50"); };
        self.rapidQuarter = function() { self.sendOverride("rapid", "25"); };

        self.spindlePlus10 = function() { self.sendOverride("spindle", "plus10"); };
        self.spindleMinus10 = function() { self.sendOverride("spindle", "minus10"); };
        self.spindlePlus1 = function() { self.sendOverride("spindle", "plus1"); };
        self.spindleMinus1 = function() { self.sendOverride("spindle", "minus1"); };
        self.spindleReset = function() { self.sendOverride("spindle", "reset"); };

        // Sub-tab navigation
        self.switchTab = function(tab) {
            self.activeSubTab(tab);
            // Initialize renderer if not yet ready
            if (!self._renderer) {
                setTimeout(function() {
                    self._initWorkspace();
                }, 50);
            } else {
                // Trigger resize in case layout changed
                self._renderer._handleResize();
            }
        };

        // =====================================================================
        // Event handlers
        // =====================================================================

        self.onBeforeBinding = function() {
            var s = self.settings.settings.plugins.grblpcb;
            if (s.ui_help_mode) {
                self.uiHelpMode(s.ui_help_mode());
            }
            self.showQuickGuide(true);
            self.showQuickGuideDetails(true);
            self.showExperiencedUsersDetails(true);
            self.jogFeed(s.default_jog_feed());
            self.spindleSpeedTarget(s.default_spindle_speed());
            self.autolevelGridX(s.autolevel_grid_x());
            self.autolevelGridY(s.autolevel_grid_y());
            self.autolevelMargin(s.autolevel_margin());
            // Populate inline generation params from settings
            self.genToolDiameter(s.tool_diameter());
            self.genToolAngle(s.tool_angle());
            self.genCutDepth(s.cut_depth());
            self.genDepthPerPass(s.depth_per_pass());
            self.genFeedRate(s.default_feed_rate());
            self.genPlungeRate(s.plunge_rate());
            self.genPasses(s.isolation_passes());
            self.genPassOverlap(s.isolation_overlap());
            self.genMaxWidth(s.isolation_max_width());
            self.genClearGaps(s.isolation_clear_gaps());
            self.genSpindleSpeed(s.default_spindle_speed());
            self.genSafeZ(s.safe_z_height());
            self.genTravelZ(s.travel_z_height());
            self.genDrillFeed(s.drill_feed());
            self.genPeckDepth(s.peck_depth());
            self.genBoardThickness(s.board_thickness());
            self.genBreakthrough(s.breakthrough_depth());
            self.genTabWidth(s.outline_tab_width());
            self.genTabHeight(s.outline_tab_height());
            self.genNumTabs(s.outline_num_tabs());
            // UV Mask Exposure
            self.genUvOffsetX(s.uv_led_offset_x());
            self.genUvOffsetY(s.uv_led_offset_y());
            self.genUvSpotSize(s.uv_spot_size());
            self.genUvExposeSpeed(s.uv_expose_speed());
            self.genUvExposeZ(s.uv_expose_z());
            self.genUvPower(s.uv_power());
        };

        self.onStartup = function() {
            self.connection.selectedConnector.subscribe(function(connector) {
                self.isGrblConnector(connector === "grbl");
            });

            // Load existing project if any
            self._refreshProject();
            // Load project list for browser
            self.refreshProjectList();
        };

        self.onAfterBinding = function() {
            // Defer workspace init until our tab is actually visible,
            // otherwise the canvas container has 0x0 dimensions.
            self._tabVisible = false;
        };

        self.onTabChange = function(current, previous) {
            // OctoPrint calls this when a top-level tab becomes visible
            if (current === "#tab_plugin_grblpcb") {
                self._tabVisible = true;
                if (!self._renderer) {
                    setTimeout(function() {
                        self._initWorkspace();
                    }, 50);
                } else {
                    self._renderer._handleResize();
                    self._renderer.render();
                }
            }
        };

        self.onDataUpdaterPluginMessage = function(plugin, data) {
            if (plugin !== "grblpcb") return;

            if (data.type === "status") {
                self.isConnected(true);
                self.isGrblConnector(true);
                self.grblState(data.state);
                
                if (data.machine_pos) {
                    self.machinePosX(data.machine_pos.x.toFixed(3));
                    self.machinePosY(data.machine_pos.y.toFixed(3));
                    self.machinePosZ(data.machine_pos.z.toFixed(3));
                }
                
                if (data.work_pos) {
                    self.workPosX(data.work_pos.x.toFixed(3));
                    self.workPosY(data.work_pos.y.toFixed(3));
                    self.workPosZ(data.work_pos.z.toFixed(3));

                    // Update machine position on workspace
                    if (self._renderer) {
                        self._renderer.updateMachinePosition(
                            data.work_pos.x, data.work_pos.y, data.work_pos.z
                        );
                    }
                }
                
                if (data.feed !== undefined) self.feedRate(data.feed);
                if (data.spindle !== undefined) self.spindleSpeed(data.spindle);
                if (data.wcs) self.currentWCS(data.wcs);
                if (data.overrides) {
                    self.feedOverride(data.overrides.feed || 100);
                    self.rapidOverride(data.overrides.rapid || 100);
                    self.spindleOverride(data.overrides.spindle || 100);
                }
            }
            else if (data.type === "autolevel_progress") {
                self.autolevelProgress(data.percent);
                self.autolevelStatus(
                    _.sprintf(gettext("Probing point %d/%d (%d%%) Z=%s"), data.index + 1, data.total, data.percent, data.point.z.toFixed(4))
                );
                // Update the specific point
                var pts = self.heightmapPoints().slice();
                if (pts[data.index]) {
                    pts[data.index] = data.point;
                    self.heightmapPoints(pts);
                }
                // Update workspace
                self._syncWorkspaceData();
            }
            else if (data.type === "alarm") {
                new PNotify({
                    title: _.sprintf(gettext("GRBL alarm %d"), data.code),
                    text: data.message + "\n\n" + gettext("The machine position may be lost. Home the machine or unlock it, then check the work zero before continuing."),
                    type: "error",
                    hide: false
                });
            }
            else if (data.type === "autolevel_complete") {
                self.autolevelProgress(100);
                var statusMsg = gettext("Probing complete! Height map ready.");
                if (data.z_drift_exceeded) {
                    var driftWarning = _.sprintf(
                        gettext("Z-axis drift of %(drift)s mm detected after probing %(points)d points. The Z motor lost steps. Zero Z again before milling and consider lowering Z acceleration ($122) or Z max rate ($112)."),
                        {drift: Math.abs(data.z_drift).toFixed(3), points: data.probed_points}
                    );
                    statusMsg = "⚠ " + driftWarning;
                    new PNotify({
                        title: gettext("Z-axis drift detected"),
                        text: driftWarning,
                        type: "error",
                        hide: false
                    });
                } else if (data.z_drift !== undefined) {
                    statusMsg += " " + _.sprintf(gettext("(Z drift: %(drift)d µm)"), {drift: Math.round(data.z_drift * 1000)});
                }
                self.autolevelStatus(statusMsg);
                self.hasHeightmap(true);
                self.heightmapPoints(data.heightmap.points);
                // Update workspace
                self._syncWorkspaceData();
            }
            else if (data.type === "alignment_progress") {
                var h = data.hole;
                self.alignmentStatus(
                    _.sprintf(gettext("Probing hole %d/%d (%d%%)"), data.index + 1, data.total, data.percent) +
                    (h.probed ? " → (" + h.measured_x.toFixed(3) + ", " + h.measured_y.toFixed(3) + ")" : "")
                );
                // Update the holes array
                var holes = self.alignmentHoles().slice();
                if (holes[data.index]) {
                    holes[data.index] = h;
                    self.alignmentHoles(holes);
                }
            }
            else if (data.type === "alignment_complete") {
                self.alignmentProbing(false);
                self.alignmentHoles(data.holes);
                if (data.all_probed) {
                    self.alignmentStatus(gettext("All holes probed! Click 'Compute Transform' to calculate alignment."));
                    // Auto-compute the transform
                    self.computeAlignment();
                } else {
                    self.alignmentStatus(gettext("Probing finished (not all holes probed)"));
                }
            }
            else if (data.type === "stream_progress") {
                self.executionCurrentLine(data.current_line);
                self.executionTotalLines(data.total_lines);
                self.executionProgress(data.percent);
                
                var errorSuffix = (data.errors && data.errors > 0) ? _.sprintf(gettext(" (%d GRBL errors)"), data.errors) : "";
                
                if (data.status === "completed") {
                    self.executionActive(false);
                    self.executionStatus(_.sprintf(gettext("Completed in %ss"), data.elapsed) + errorSuffix);
                    self._refreshProject();
                } else if (data.status === "stopped") {
                    self.executionActive(false);
                    self.executionStatus(_.sprintf(gettext("Stopped at line %d"), data.current_line) + errorSuffix);
                } else if (data.status === "tool_change") {
                    self.executionStatus(_.sprintf(gettext("⚠️ TOOL CHANGE required at line %d. Click Confirm when ready."), data.current_line + 1));
                } else if (data.status === "pause") {
                    self.executionStatus(_.sprintf(gettext("⏸ Program pause at line %d. Click Confirm to continue."), data.current_line + 1));
                } else if (data.status === "progress") {
                    var eta = data.eta > 0 ? " " + _.sprintf(gettext("ETA: %ds"), Math.round(data.eta)) : "";
                    self.executionStatus(
                        _.sprintf(gettext("Line %d/%d (%d%%)"), data.current_line, data.total_lines, data.percent) + eta + errorSuffix
                    );
                } else if (data.status === "started") {
                    self.executionActive(true);
                    self.executionStatus(_.sprintf(gettext("Streaming started: %s"), self.executionOperationLabel || data.operation));
                }
            }
        };
    }

    // Register the view model
    OCTOPRINT_VIEWMODELS.push({
        construct: GrblPcbViewModel,
        dependencies: ["settingsViewModel", "loginStateViewModel", "printerStateViewModel", "connectionViewModel"],
        elements: ["#tab_plugin_grblpcb", "#navbar_plugin_grblpcb"]
    });

});
