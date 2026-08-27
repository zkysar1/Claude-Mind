#!/usr/bin/env bash
# State-update audit — Tier 1a hot-path extraction.
# Subcommands: velocity|backpressure|temporal-credit|relative-advantage|
#              execution-feedback|run-all
# Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 1a #5).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/state-update-audit.py" "$@"
