#!/usr/bin/env bash
# precheck-sentinel-battery.sh — thin wrapper for precheck-sentinel-battery.py
# (3). One call enumerating all precheck force-gate sentinels; see
# the .py docstring. Fail-open by design: the battery must never block the
# loop, so any wrapper-level failure still exits 0 with a structured line
# (guard-614).
set -uo pipefail
_SELF="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$_SELF/_paths.sh" 2>/dev/null || true
python3 "$_SELF/precheck-sentinel-battery.py" "$@" \
  || echo '[sentinel-battery] wrapper_failed — fall back to per-phase wm-read calls'
exit 0
