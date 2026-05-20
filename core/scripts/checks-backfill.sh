#!/usr/bin/env bash
# checks-backfill — Tier 1b one-time migration.
# Backfills verification.checks[] from templates or legacy completion_check.
# Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 1b #3).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/checks-backfill.py" "$@"
