#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# loop-state-save.sh — Single-writer wrapper for iteration-checkpoint.json ().
#
# Subcommands: init | update | clear | read. Dispatches to loop-state-save.py
# which owns the schema, atomic write, and validation. See the .py docstring
# for the typed-key list and rb-428 lineage.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/loop-state-save.py" "$@"
