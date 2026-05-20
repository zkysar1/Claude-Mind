#!/usr/bin/env bash
# Thin wrapper for predicate.py — evaluate structured preconditions.
#
# Usage:
#   predicate-eval.sh --goal <goal-id> [--types structured|all]
#   predicate-eval.sh --predicate '<json>'
#
# Exit codes:
#   0 — all evaluated predicates passed (or none declared — see vacuous-truth
#       contract below)
#   1 — at least one failed
#   2 — goal not found, bad JSON, or usage error
#
# VACUOUS-TRUTH CONTRACT (): Exit 0 is ambiguous for callers that
# treat it as "the goal is unblocked". `all([])` returns True, so a goal with
# zero structured preconditions returns exit 0 from --goal mode. Shell callers
# that need "at least one predicate was evaluated" must inspect the JSON
# `results` array length BEFORE trusting the exit code. goal-selector.py L981
# demonstrates the correct upstream pattern (`if struct_pcs:` pre-filter), and
# precondition-defer-recheck.py mirrors it (struct_pc_count==0 → SKIP, NOT
# clear). aspirations-precheck Phase 0.5b.3 () was specifically
# rewritten as a bash-enforced sweep to avoid the exit-code-alone trap.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
GOAL_NORMALIZE_TARGET=--goal source "$CORE_ROOT/scripts/_goal-arg-normalize.sh"
exec python3 "$CORE_ROOT/scripts/predicate.py" "$@"
