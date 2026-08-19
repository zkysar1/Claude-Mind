#!/usr/bin/env bash
# unit-claim.sh — unit-level claim for deliberately multi-unit goals ().
#
# A deliberately multi-unit goal ("wire the 11 templates, one at a time, one PR
# each") is NON-TERMINAL: a Body claims it, does ONE unit, and RELEASES. The goal
# claim is free again within minutes and nothing records which UNIT is in flight,
# so two Bodies can build the same unit — measured 2026-08-19, one full unit of
# work wasted (PR #33 closed as a duplicate of PR #32).
#
# Usage:
#   unit-claim.sh acquire <goal-id> <unit> [--force "<why>"] [--json]
#   unit-claim.sh release <goal-id> <unit> [--json]
#   unit-claim.sh status  <goal-id> [--json]
#
# EXIT CODES ARE THE SIGNAL on acquire:
#   0 — acquired (or already held by THIS Body). Start the unit.
#   1 — REFUSED: another Body holds an unexpired claim. Do NOT start this unit.
#   2 — plumbing failure (board unreadable/unwritable). Never silently "free".
#
# The lease is multi_agent.claim_timeout_hours from core/config/aspirations.yaml
# — the SAME lease the goal-level claim uses, so a unit claim can never outlive
# the goal claim containing it.
#
# Identity is the SESSION (MIND_SID), not the agent: a worker Body and its
# reducer are both `alpha`, so an agent-name comparison cannot separate them
# (same reason coordination_merge._merge_goal carries a _diff_body branch).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/_paths.sh"

case "${1-}" in
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
esac

exec python3 "$HERE/unit_claim.py" "$@"
