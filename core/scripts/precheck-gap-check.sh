#!/usr/bin/env bash
# precheck-gap-check.sh — is Phase 0-1 (aspirations-precheck) actually running?
# Prints one verdict line (+ a loud banner when >= 1 iteration closed since the
# precheck last started). Always exit 0 — detector, never a gate. Called from
# iteration-close.sh (productivity-check, above the ITERATION COMPLETE
# imperative) and compact-restore-slots.sh (post-autocompact resume).
# See core/scripts/precheck-gap-check.py for the measurement and its origin.
set -uo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
python3 "$CORE_ROOT/scripts/precheck-gap-check.py" "$@" || true
exit 0
