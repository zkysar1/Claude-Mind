#!/usr/bin/env bash
# verify-check-eval — Tier 1b declarative verification.
# Sister to predicate.sh. Evaluates verification.checks[] for Phase 5 completion.
# Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 1b #1).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/verify-check-eval.py" "$@"
