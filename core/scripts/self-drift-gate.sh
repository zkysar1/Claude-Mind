#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Canonical entry point for self-drift-gate.py. DO NOT invoke the .py
# directly from SKILL.md pseudocode — on Windows `bash core/scripts/X.py`
# parses the Python docstring as shell and silently no-ops under `|| true`.
# This wrapper sources the standard _paths.sh + _platform.sh preamble so
# python3 resolves via the shim on Windows and natively on Linux/Mac.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/self-drift-gate.py" "$@"
