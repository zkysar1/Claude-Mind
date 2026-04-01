#!/usr/bin/env bash
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
    export AYOAI_SHELL="$(cygpath -m "$(which bash)")"
else
    export AYOAI_SHELL="$(which bash)"
fi
exec python3 "$CORE_ROOT/scripts/background-jobs.py" "$@"
