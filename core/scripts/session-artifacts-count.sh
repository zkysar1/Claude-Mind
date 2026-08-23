#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
set -euo pipefail
# Require MIND_AGENT explicitly. Without it, _paths.py falls back to
# a first-available-conf loop that could probe the wrong agent's WM.
if [[ -z "${MIND_AGENT:-}" ]]; then
    echo "[session-artifacts-count] MIND_AGENT must be set" >&2
    exit 2
fi
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/session_artifacts_count.py" "$@"
