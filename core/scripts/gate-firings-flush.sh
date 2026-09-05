#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# gate-firings-flush.sh — wrapper for gate-firings-flush.py ().
#
# Exists so SKILL.md pseudocode can invoke the flusher as `bash
# core/scripts/gate-firings-flush.sh` instead of naming the .py directly
# (guard-350: `bash <file>.py` makes bash parse the Python docstring as shell,
# every line errors, and a trailing `|| true` masks it as exit 0).
#
# Flags pass straight through: --meta-dir, --min-interval-seconds (default 300),
# --burst-records (default 200), --force, --dry-run. The interval gate lives in
# the .py, so callers may invoke this every cycle without bounding it themselves.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/gate-firings-flush.py" "$@"
