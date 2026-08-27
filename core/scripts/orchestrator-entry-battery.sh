#!/usr/bin/env bash
# orchestrator-entry-battery.sh — thin wrapper for orchestrator-entry-battery.py
# (). One call enumerating the aspirations/SKILL.md orchestrator
# entry-phase checks; see the .py docstring. Fail-open by design: the battery
# must never block the loop, so any wrapper-level failure still exits 0 with a
# structured line (guard-614).
set -uo pipefail
_SELF="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$_SELF/_paths.sh" 2>/dev/null || true
python3 "$_SELF/orchestrator-entry-battery.py" "$@" \
  || echo '[entry-battery] wrapper_failed — fall back to per-phase entry checks'
exit 0
