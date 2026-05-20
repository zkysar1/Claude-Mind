#!/usr/bin/env bash
# dependent-unblock.sh — bash wrapper for Phase 5 dependent-goal unblocking.
# Replaces the LLM-orchestrated "Unblock Dependent Goals (with Output
# Passing)" loop in aspirations-verify/SKILL.md Phase 5 with a single
# bash-enforced call (rb-428 lineage; siblings: experience-archive-goal.sh,
# findings-gate.sh, decision-rules-append.sh, loop-state-save.sh,
# skill-quality-score.sh, journal-append.sh).
#
# Usage:
#   bash dependent-unblock.sh --goal <completed-goal-id> --summary "<text>" [--dry-run]
#
# See dependent-unblock.py docstring for full semantics, exit codes, and
# stdout JSON shape.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/dependent-unblock.py" "$@"
