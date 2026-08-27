#!/usr/bin/env bash
# unchecked-write-ratchet.sh — advisory ratchet over the unchecked-write audit.
# Wired into /verify-learning (). Tracks the STRICT `unverified` count.
# A `skipped` audit verdict (empty population) leaves the baseline UNTOUCHED.
# Exit 0 always unless VERIFY_LEARNING_DRIFT_HARD_GATE=1.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/unchecked-write-ratchet.py" "$@"
