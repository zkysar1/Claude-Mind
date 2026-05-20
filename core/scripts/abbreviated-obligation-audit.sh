#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Abbreviated-obligation audit — Tier 1a hot-path extraction.
# Replaces aspirations-learning-gate/SKILL.md Phase 9.5d.
# Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 1a #6).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/abbreviated-obligation-audit.py" "$@"
