#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Capability gate — block user-routing for blockers when an agent capability matches.
# See capability-gate.py for full docs. Called by aspirations-execute CREATE_BLOCKER Step 2.6.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/capability-gate.py" "$@"
