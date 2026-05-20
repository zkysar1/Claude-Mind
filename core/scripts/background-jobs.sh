#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Background external job tracker — thin wrapper around background-jobs.py.
# Tracks long-running OS processes (hours+) so the aspirations loop can
# monitor them via recurring goals and collect results on completion.
# Complements pending-agents.sh (which tracks short-lived Claude sub-agents).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
# Export the current shell path so the Python script can use the same bash
# for subprocess calls (avoids WSL bash on Windows where Git Bash is intended).
# Use cygpath to convert MSYS /usr/bin/bash to a Windows-readable path.
if command -v cygpath &>/dev/null; then
    export MIND_SHELL="$(cygpath -m "$(which bash)")"
else
    export MIND_SHELL="$(which bash)"
fi
GOAL_NORMALIZE_TARGET=--goal source "$CORE_ROOT/scripts/_goal-arg-normalize.sh"
exec python3 "$CORE_ROOT/scripts/background-jobs.py" "$@"
