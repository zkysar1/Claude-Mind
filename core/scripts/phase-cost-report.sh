#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Phase-cost report — Tier 0 measurement infrastructure.
# Reads {agent}/session/execution-diary.jsonl, pairs phase_start/phase_end
# markers, emits per-phase wall-clock cost JSON.
# See plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/phase-cost-report.py" "$@"
