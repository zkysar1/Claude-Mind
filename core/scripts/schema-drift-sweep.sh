#!/usr/bin/env bash
# schema-drift-sweep.sh — thin wrapper around schema-drift-sweep.py
# Same arguments. Routes through `py -3` to bypass the Microsoft Store
# Python stub on Windows (rb-370 / guard-335).
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# py -3 on Windows, python3 elsewhere: never a bare `py -3` (, guard-1098).
source "$SCRIPT_DIR/_python_launcher.sh"
PYLAUNCH="$(rt_python_launcher)" || PYLAUNCH=python3
exec $PYLAUNCH "$SCRIPT_DIR/schema-drift-sweep.py" "$@"
