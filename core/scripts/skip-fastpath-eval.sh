#!/usr/bin/env bash
# skip-fastpath-eval — Tier 2 utility extraction.
# Phase 4.0 SKIP decision: RETRY|PROVISION|CREATE_BLOCKER.
# Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 2 #4).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/skip-fastpath-eval.py" "$@"
