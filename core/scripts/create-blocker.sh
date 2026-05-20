#!/usr/bin/env bash
# CREATE_BLOCKER orchestrator — Tier 1a hot-path extraction.
# Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 1a #4).
# Wraps: blocker-create-gate + conclusion-record + capability-gate +
#        aspirations-add-goal + wm-set(known_blockers).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/create-blocker.py" "$@"
