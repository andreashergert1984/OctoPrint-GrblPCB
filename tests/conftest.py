"""Shared fixtures for OctoPrint-GrblPCB tests."""
# OctoPrint's server imports the file manager before the printer module;
# importing octoprint.printer first runs into a circular import in core.
import pathlib
import sys

import octoprint.filemanager  # noqa: F401

# Test helpers (gcode_reader) live next to the tests
sys.path.insert(0, str(pathlib.Path(__file__).parent))
