#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# handoff-yaml-build — Tier 2 utility extraction.
# Assembles <agent>/session/handoff.yaml from structured JSON.
# Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 2 #1).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/handoff-yaml-build.py" "$@"
