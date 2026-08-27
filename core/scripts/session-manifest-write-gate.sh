#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Session Manifest Write Gate — wrapper.
#
# Usage:
#   bash core/scripts/session-manifest-write-gate.sh <path> [--output json|human]
#
# Exit codes:
#   0  — write allowed (out-of-scope, registered, or unregistered+reader/unknown)
#   1  — write refused (unregistered + autonomous/assistant)
#
# See session-manifest-write-gate.py for the full decision table.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/session-manifest-write-gate.py" "$@"
