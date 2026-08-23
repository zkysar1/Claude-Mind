#!/usr/bin/env bash
# experience-orphan-ratchet.sh — advisory drift check with baseline ratchet for
# orphaned experience traces (). Wired into /verify-learning.
# Exit 0 always unless VERIFY_LEARNING_DRIFT_HARD_GATE=1.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/experience-orphan-ratchet.py" "$@"
