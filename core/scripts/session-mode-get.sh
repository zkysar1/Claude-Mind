#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Read agent-mode — inlined for ~5x speedup vs the python wrapper.
#
# Output parity with `session.py mode get`: prints NO_AGENT (no agent bound),
# the trimmed file contents when agent-mode exists, or "reader" (DEFAULT_MODE)
# when the file is absent. Always followed by a newline.
#
# DEFAULT_MODE mirrored from core/scripts/session.py:48. When changing the
# default mode, update BOTH places.
#
# Inlined in PR 6 alongside session-state-get/session-signal-exists — see
# that file's header for the rationale and the inline-vs-daemon decision.
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
mode_file="$(_agent_dir "${MIND_AGENT}")/session/agent-mode"

if [ -f "$mode_file" ]; then
    val="$(tr -d '[:space:]' < "$mode_file")"
    echo "$val"
else
    echo "reader"
fi
