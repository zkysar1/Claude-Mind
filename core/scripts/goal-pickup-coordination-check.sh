#!/usr/bin/env bash
# Advisory same-surface-race probe at goal-pickup: warns when a goal's surface
# was committed in the last N hours (catches the partner-already-shipped race
# that the in_flight partner-claim filter and the commit-time concurrent-partner
# filter both miss — the canonical 2026-05-13 incident). Detective only — never
# mutates goal state, always exits 0. See goal-pickup-coordination-check.py
# docstring for full semantics and the  (US-03) lineage.
# Sibling pattern: defer-drift-check.sh, unblock-parent-status-sweep.sh.
#
# Usage: goal-pickup-coordination-check.sh --goal-id g-NNN-NN \
#          [--source world|agent] [--since-hours N] [--output json|human]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/goal-pickup-coordination-check.py" "$@"
