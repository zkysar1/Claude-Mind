#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Background external job tracker — thin wrapper around background-jobs.py.
# Tracks long-running OS processes (hours+) so the aspirations loop can
# monitor them via recurring goals and collect results on completion.
# Complements pending-agents.sh (which tracks short-lived Claude sub-agents).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
# MIND_SHELL is set by _paths.sh (sourced above), which now prefers the
# login-launcher bin/bash.exe over the raw usr/bin/bash.exe (rb-1472). The
# previous re-export here re-derived `which bash` -> usr/bin/bash.exe and
# UNCONDITIONALLY overwrote _paths.sh's value with the fragile one. Removed so
# _paths.sh stays the single source of truth for the shell path.
GOAL_NORMALIZE_TARGET=--goal source "$CORE_ROOT/scripts/_goal-arg-normalize.sh"
exec python3 "$CORE_ROOT/scripts/background-jobs.py" "$@"
