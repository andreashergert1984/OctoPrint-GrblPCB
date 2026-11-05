#!/bin/sh
# Start OctoPrint; with GRBL_SIMULATOR=1 also start the GRBL simulator on
# /tmp/ttyGRBL and offer it as an additional serial port.
set -e

BASEDIR=/data

if [ "${GRBL_SIMULATOR:-0}" = "1" ]; then
    python /plugin/tools/grbl_simulator.py /tmp/ttyGRBL &
    octoprint --basedir "$BASEDIR" config set --json \
        plugins.serial_connector.additionalPorts '["/tmp/ttyGRBL"]'
fi

exec octoprint serve --basedir "$BASEDIR" --host 0.0.0.0 --port 5000
