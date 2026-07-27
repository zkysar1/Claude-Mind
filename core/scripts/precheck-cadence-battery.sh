#!/usr/bin/env bash
# precheck-cadence-battery.sh — thin wrapper for precheck-cadence-battery.py
# (, fix for ). One call running all six deferrable
# skill-invocation cadence gate checks and reporting which FIRE; see the .py
# docstring. Fail-open by design: the battery must never block the loop, so any
# wrapper-level failure still exits 0 with a structured line (guard-614).
set -uo pipefail
_SELF="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$_SELF/_paths.sh" 2>/dev/null || true
python3 "$_SELF/precheck-cadence-battery.py" "$@" \
  || echo '[cadence-battery] wrapper_failed — fall back to per-phase cadence checks'
exit 0
