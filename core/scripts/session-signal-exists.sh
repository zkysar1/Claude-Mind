#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Check if a signal marker file exists — inlined for ~5x speedup vs python.
#
# Output parity with `session.py signal exists <name>`:
#   exit 0 — signal file exists
#   exit 1 — signal file does not exist
#   exit 2 — invalid signal name (stderr message)
#
# Requires MIND_AGENT to be set; absence prints the same error as the python
# wrapper's require_agent() and exits 1 (NOT 2 — that's for invalid names).
#
# VALID_SIGNALS mirrored from core/scripts/session.py:36. When adding a new
# signal, update BOTH places. The python list is authoritative for writers
# (session-signal-set.sh / session-signal-clear.sh still route through
# session.py); this case statement is the readers' parallel mirror, which
# matches what interruptible-sleep.sh already does with raw [ -f ] checks.
set -euo pipefail

if [ -z "${MIND_AGENT:-}" ]; then
    echo "Error: no agent active (MIND_AGENT not set). Use /start <name> first." >&2
    exit 1
fi

if [ $# -lt 1 ]; then
    echo "ERROR: signal name required" >&2
    exit 2
fi

name="$1"

# LOAD-BEARING MIRROR: this case statement enumerates VALID_SIGNALS from
# core/scripts/session.py:36. The python file remains the authoritative
# source — writers (session-signal-set/clear.sh) route through python.
# This is the readers' parallel mirror (matches interruptible-sleep.sh's
# raw [ -f ] checks). When adding a new signal in session.py, update
# THIS case statement AND the error message below in the same edit.
# Drift surfaces loudly as "Invalid signal name" at runtime when the new
# signal is queried via this script. The test
# test_signal_exists_all_valid_names_recognized in
# mind_api/tests/test_runtime_tier_c.py has the same mirrored list and
# locks against silent drift in the test suite itself.
case "$name" in
    loop-active|stop-loop|stop-requested|blocker-cleared|pq-resolved|board-activity|email-received|goal-claim-released)
        ;;
    *)
        echo "ERROR: Invalid signal name '$name'. Must be one of: blocker-cleared, board-activity, email-received, goal-claim-released, loop-active, pq-resolved, stop-loop, stop-requested" >&2
        exit 2;;
esac

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
# Phase 2.5.C: sync with _paths.sh AGENTS_PARENT_DIR
_APD="agents"
_agent_dir() { if [ -n "$_APD" ]; then printf '%s/%s/%s' "$PROJECT_ROOT" "$_APD" "$1"; else printf '%s/%s' "$PROJECT_ROOT" "$1"; fi; }

if [ -e "$(_agent_dir "${MIND_AGENT}")/session/$name" ]; then
    exit 0
else
    exit 1
fi
