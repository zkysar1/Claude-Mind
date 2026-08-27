#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# session-manifest-orphan-ratchet.sh — advisory orphan-count check with baseline ratchet.
# Wired into /verify-learning. Exit 0 always unless VERIFY_LEARNING_ORPHAN_HARD_GATE=1.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/session-manifest-orphan-ratchet.py" "$@"
