#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# cleanup-stale-bindings.sh — Single writer for stale .active-agent-* cleanup.
#
# Called from stop-hook.sh (every turn end) and session-save-id.sh (every
# SessionStart). Both surfaces previously held an inline copy of the same
# 3-signal predicate — extracted here to eliminate drift risk between them.
#
# DELETE PREDICATE (ALL THREE must hold; any single signal "live" skips):
#   1. File mtime > 24h old (gentle TTL — recent files are obviously live)
#   2. EITHER running-session-id absent OR its content != THIS binding's SID
#      (autonomous mode writes running-session-id; observer modes don't)
#   3. runner-heartbeat is stale per heartbeat-stale.sh (canonical liveness
#      gate; only autonomous mode ticks the heartbeat)
#
# WHY all three: the old single-signal predicate {mtime>24h && no
# running-session-id-file} deleted zeta's binding on 2026-05-12T08:47 while
# zeta was actively running (running-session-id momentarily absent during a
# graceful-stop write window). That deletion opened the door for the bravo
# `claude --continue` collision at 09:56 — bravo's /start saw no binding at
# the inherited SID and silently took it. SID-content match + heartbeat
# freshness are independent signals; requiring BOTH eliminates the
# spurious-delete class.
#
# DO NOT WEAKEN BACK to a single-signal check.
# DO NOT INLINE BACK into stop-hook.sh or session-save-id.sh — drift between
# the two copies was the bug class this extraction prevents.
#
# OBSERVER-MODE NOTE: assistant/reader sessions write neither
# running-session-id (signal 2) nor a runner heartbeat (signal 3), so for
# them signal 1 (mtime>24h) is effectively the sole signal. The CALLER is
# responsible for refreshing mtime on its own binding before invoking this
# script — see stop-hook.sh's `touch -c` immediately above its invocation.
# Without that touch, a 24h+ observer session would delete its own binding.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# Phase 2.5.C: sync with _paths.sh AGENTS_PARENT_DIR
_APD="agents"
# Phase 2.6: sync with _paths.sh SESSIONS_DIRNAME
_SDN="sessions"
_agents_root() { if [ -n "$_APD" ]; then printf '%s/%s' "$PROJECT_ROOT" "$_APD"; else printf '%s' "$PROJECT_ROOT"; fi; }
_agent_dir() { if [ -n "$_APD" ]; then printf '%s/%s/%s' "$PROJECT_ROOT" "$_APD" "$1"; else printf '%s/%s' "$PROJECT_ROOT" "$1"; fi; }

# Shared predicate: should we delete the binding for (agent=$1, sid=$2)?
# Returns 0 (yes-delete) when all three signals fail; non-zero (keep) otherwise.
# The mtime test is OUTSIDE because mtime source differs per layout.
_should_delete_binding() {
    local _BA="$1"
    local _BIND_SID="$2"
    # Signal 2: running-session-id absent OR doesn't match the bound SID
    local _CUR_SID
    _CUR_SID=$(cat "$(_agent_dir "$_BA")/session/running-session-id" 2>/dev/null | tr -d '\r\n')
    [ "$_CUR_SID" = "$_BIND_SID" ] && return 1
    # Signal 3: runner-heartbeat is stale
    local _HB
    _HB=$(MIND_AGENT=$_BA bash "$SCRIPT_DIR/heartbeat-stale.sh" 2>/dev/null || echo fresh)
    [ "$_HB" = "fresh" ] && return 1
    return 0
}

# Legacy sweep: .active-agent-<SID> at PROJECT_ROOT (pre-Phase-2.6).
for _AF in "$PROJECT_ROOT"/.active-agent-*; do
    [ -f "$_AF" ] || continue
    [ -z "$(find "$_AF" -maxdepth 0 -mmin +1440 2>/dev/null)" ] && continue
    _BA=$(cat "$_AF" 2>/dev/null | tr -d '\r\n')
    [ -n "$_BA" ] || { rm -f "$_AF"; continue; }
    _BIND_SID="${_AF##*/.active-agent-}"
    if _should_delete_binding "$_BA" "$_BIND_SID"; then
        rm -f "$_AF"
    fi
done

# Phase 2.6 sweep: agents/<name>/sessions/<sid>/binding.yaml.
# Same 3-signal predicate. On delete, removes the entire per-session dir
# so co-located scratch / iteration-checkpoint / watchdog-prev-state stale
# crumbs go with it (they were the per-session-dir purpose — none survives
# a stale-binding sweep).
for _ASR in "$(_agents_root)"/*; do
    [ -d "$_ASR" ] || continue
    _BA="${_ASR##*/}"
    [ -d "$_ASR/$_SDN" ] || continue
    for _SD in "$_ASR/$_SDN"/*; do
        [ -d "$_SD" ] || continue
        _BFILE="$_SD/binding.yaml"
        [ -f "$_BFILE" ] || continue
        # Signal 1: binding.yaml mtime > 24h old
        [ -z "$(find "$_BFILE" -maxdepth 0 -mmin +1440 2>/dev/null)" ] && continue
        _BIND_SID="${_SD##*/}"
        if _should_delete_binding "$_BA" "$_BIND_SID"; then
            rm -rf "$_SD"
        fi
    done
done

# ─── bash-inject sentinel sweep (plan v1 step 0.3, 2026-05-19) ───────────────
# Companion sweep for the bash-agent-inject.py one-shot sentinels at
# core/logs/bash-inject-sentinels/<sid>. These zero-byte files track SIDs
# that hit the PreToolUse[Bash] hook without a resolvable agent binding —
# the sentinel suppresses log-line spam (one record per unique NO_AGENT SID,
# not one per Bash call). Without a sweep they accumulate forever.
#
# TTL: 24h (matches the .active-agent-* sweep above). A SID with no agent
# binding that hasn't fired the hook in 24h is functionally dead — the
# Claude Code session is no longer active, so the log-spam-prevention
# purpose has lapsed. If the SID resumes activity tomorrow it will simply
# re-create the sentinel on its next Bash call (the file is one-shot).
#
# Predicate is SIMPLER than the .active-agent sweep above: bash-inject
# sentinels have NO associated running-session-id or heartbeat (they're
# precisely the "no agent" case). The 24h mtime threshold IS the only
# signal — same conservative-on-error pattern (a SID still active today
# has touched its sentinel within the last 24h or hasn't created one yet).
_SENTINEL_DIR="$PROJECT_ROOT/core/logs/bash-inject-sentinels"
if [ -d "$_SENTINEL_DIR" ]; then
    for _S in "$_SENTINEL_DIR"/*; do
        [ -f "$_S" ] || continue
        [ -z "$(find "$_S" -maxdepth 0 -mmin +1440 2>/dev/null)" ] && continue
        rm -f "$_S"
    done
fi

# ─── Legacy-location bash-inject sentinels at PROJECT_ROOT ───────────────────
# Pre-step-0.13 (2026-05-19), bash-agent-inject.py wrote sentinels to
# `PROJECT_ROOT/.bash-inject-no-binding-<sid>`. Any stragglers from before
# the relocation get swept here on the same 24h TTL — eventually they all
# expire and this loop can be retired (target: Phase 1 deletion after a
# week of zero legacy stragglers observed).
for _S in "$PROJECT_ROOT"/.bash-inject-no-binding-*; do
    [ -f "$_S" ] || continue
    [ -z "$(find "$_S" -maxdepth 0 -mmin +1440 2>/dev/null)" ] && continue
    rm -f "$_S"
done
