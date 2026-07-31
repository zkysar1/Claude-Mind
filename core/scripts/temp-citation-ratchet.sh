#!/usr/bin/env bash
# temp-citation-ratchet.sh — advisory drift check with baseline ratchet for
# agents/*/temp/ citations in the durable knowledge stores ().
# Sibling of experience-orphan-ratchet.sh; wired into /verify-learning.
# Exit 0 always unless VERIFY_LEARNING_DRIFT_HARD_GATE=1.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/temp-citation-ratchet.py" "$@"
