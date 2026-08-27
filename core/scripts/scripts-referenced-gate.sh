#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Scripts-Referenced Gate — thin wrapper for scripts-referenced-gate.py.
# Flags core/scripts/*.sh|*.py files with no live reference in
# skills/config/rules/settings/other-scripts. Runs on demand or monthly
# to catch scripts left behind after refactors.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/scripts-referenced-gate.py" "$@"
