#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Read agent-mode — inlined for ~5x speedup vs the python wrapper.
#
# Output parity with `session.py mode get`: prints NO_AGENT (no agent bound),
# the session's own binding mode when its worker /stop LANDED it, the trimmed
# file contents when agent-mode exists, or "reader" (DEFAULT_MODE) when the
# file is absent. Always followed by a newline.
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
# Phase 2.6: sync with _paths.sh SESSIONS_DIRNAME
_SDN="sessions"
_agent_dir() { if [ -n "$_APD" ]; then printf '%s/%s/%s' "$PROJECT_ROOT" "$_APD" "$1"; else printf '%s/%s' "$PROJECT_ROOT" "$1"; fi; }
mode_file="$(_agent_dir "${MIND_AGENT}")/session/agent-mode"

# A session its worker /stop LANDED reports its own binding's mode, because the
# agent-wide file belongs to the box. The rule is _session_binding.landed_mode_in,
# mirrored here by hand: a forked working-memory.yaml, and a binding mode of
# reader or assistant. Anything unreadable falls through to the agent-wide read,
# the answer before the landing existed. An empty MIND_SID only skips this
# check: it says nothing about the session, so it must not pick a mode (guard-6178).
case "${MIND_SID:-}" in
    ""|*/*|*..*|*[[:space:]]*) ;;
    *)
        _sd="$(_agent_dir "${MIND_AGENT}")/$_SDN/$MIND_SID"
        if [ -f "$_sd/working-memory.yaml" ] && [ -f "$_sd/binding.yaml" ]; then
            _bm="$(sed -n '/^mode:/{s/^mode:[[:space:]]*//p;q;}' "$_sd/binding.yaml" 2>/dev/null)" || _bm=""
            _q="'\""
            _bm="${_bm%%[[:space:]]*}"
            _bm="${_bm//[$_q]/}"
            case "$_bm" in reader|assistant) echo "$_bm"; exit 0 ;; esac
        fi
        ;;
esac

if [ -f "$mode_file" ]; then
    val="$(tr -d '[:space:]' < "$mode_file")"
    echo "$val"
else
    echo "reader"
fi
