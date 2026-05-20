#!/usr/bin/env bash
# schema-drift-sweep.sh — thin wrapper around schema-drift-sweep.py
# Same arguments. Routes through `py -3` to bypass the Microsoft Store
# Python stub on Windows (rb-370 / guard-335).
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec py -3 "$SCRIPT_DIR/schema-drift-sweep.py" "$@"
