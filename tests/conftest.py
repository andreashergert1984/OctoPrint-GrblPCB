# -*- coding: utf-8 -*-
"""Shared fixtures for OctoPrint-GrblPCB tests."""
# OctoPrint's server imports the file manager before the printer module;
# importing octoprint.printer first runs into a circular import in core.
import octoprint.filemanager  # noqa: F401
import pytest
