#!/usr/bin/env bash
# world-script-root-derivation-ratchet.sh — advisory root-derivation drift check with baseline ratchet.
# Wired into /verify-learning (). Exit 0 always unless VERIFY_LEARNING_DRIFT_HARD_GATE=1.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/world-script-root-derivation-ratchet.py" "$@"
