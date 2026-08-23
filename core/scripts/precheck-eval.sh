#!/usr/bin/env bash
# Precheck evaluator — Tier 1a hot-path extraction.
# Subcommands: run-all | zombies | pipeline-depth | hypothesis-health
#              | accuracy | consolidation | cycles | user-goals
# See plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 1a #1).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/precheck-eval.py" "$@"
