#!/usr/bin/env bash
# Reflect bookkeeping — Tier 1a hot-path extraction.
# Subcommands: encoding-score|dual-classification|convention-routing|
#              entity-normalize|context-gap|utilization-delta|batch-micro|run-all
# Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 1a #2).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/reflect-bookkeeping.py" "$@"
