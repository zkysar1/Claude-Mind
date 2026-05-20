#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Signal-Lifecycle Gate — thin wrapper for signal-lifecycle-gate.py.
# Parses session_signals inventory from aspirations-loop-digest.md and
# verifies init/increment/reset completeness + cadence single-writer.
# Would have caught F1 (consecutive_blocked_sleeps never reset) and
# F3 (last_strategic_scan phantom read).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/signal-lifecycle-gate.py" "$@"
