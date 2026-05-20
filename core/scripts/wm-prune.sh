#!/usr/bin/env bash
# wm-prune.sh — wrapper for wm.py prune (mid-session working-memory pruning).
#
# Cadence-tracker protection: wm.py defines CADENCE_TRACKER_PATTERNS (regex
# allowlist for slot names like last_fresh_eyes_review, last_evolution_at_time,
# last_strategic_scan_tick, last_*_check, last_*_scan, last_*_fire). Matching
# slots are NEVER evicted — cadence trackers are stale BY DESIGN (fire every
# N goals or N hours, often much longer than evict_threshold_minutes). See
#  (2026-04-21) for the incident: wm-prune evicted last_fresh_eyes_review
# at 132 min age and the next cadence-check would have duplicate-fired.
# Regression test: core/scripts/tests/test-wm-prune-cadence-protection.sh.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/wm.py" prune "$@"
