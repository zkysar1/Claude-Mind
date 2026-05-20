#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Read agent-state — inlined for ~5x speedup vs the python wrapper.
#
# Output parity with `session.py state get`: prints NO_AGENT (no agent bound),
# UNINITIALIZED (agent bound but no agent-state file), or the trimmed file
# contents (RUNNING / IDLE). Always followed by a newline.
#
# Inlined in PR 6 because this script is in the hot path of the session-start
# protocol, hooks, and every loop iteration — paying the python interpreter
# startup cost (~300ms on Windows) for a single file read is wasteful. The
# fallback contract used elsewhere (rc=3 → exec python3) doesn't apply: there
# is no daemon endpoint and no behavior worth deferring to python.
set -euo pipefail

if [ -z "${MIND_AGENT:-}" ]; then
    echo "NO_AGENT"
    exit 0
fi

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
# Phase 2.5.C: sync with _paths.sh AGENTS_PARENT_DIR
_APD="agents"
_agent_dir() { if [ -n "$_APD" ]; then printf '%s/%s/%s' "$PROJECT_ROOT" "$_APD" "$1"; else printf '%s/%s' "$PROJECT_ROOT" "$1"; fi; }
state_file="$(_agent_dir "${MIND_AGENT}")/session/agent-state"

if [ -f "$state_file" ]; then
    # tr strips all whitespace including \r (Windows line-ending defense)
    # then echo prints the trailing newline to match python's print()
    val="$(tr -d '[:space:]' < "$state_file")"
    echo "$val"
else
    echo "UNINITIALIZED"
fi
