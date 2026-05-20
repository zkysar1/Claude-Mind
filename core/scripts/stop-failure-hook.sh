#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# StopFailure hook — crash recovery breadcrumb for context exhaustion
#
# Fires when the session cannot continue (context exhaustion, API error).
# StopFailure is NOTIFICATION-ONLY: stdout and exit code are ignored.
# Cannot block the stop — but can save state for the next session to detect.
#
# Writes <agent>/session/crash-marker so /boot can report the abnormal shutdown.

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"

# --- Read stdin ---
STDIN_JSON=$(cat)

# --- Resolve agent (same pattern as stop-hook.sh) ---
HOOK_SID=$(printf '%s' "$STDIN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
HOOK_AGENT=""
if [ -n "$HOOK_SID" ] && [ -f "$PROJECT_ROOT/.active-agent-$HOOK_SID" ]; then
    HOOK_AGENT=$(cat "$PROJECT_ROOT/.active-agent-$HOOK_SID" 2>/dev/null | tr -d '\r\n')
fi

# Do NOT fall back to $AGENT_NAME from _paths.sh — it resolves via
# MIND_AGENT env, which may be unset at hook time. Historically a
# shared-file fallback caused cross-agent contamination (rb-386); writing
# this session's crash-marker to another agent's session dir is the same
# class of bug. If HOOK_AGENT stays empty, the `if [ -n "$HOOK_AGENT" ]`
# guard below skips the crash-marker write entirely — a missed breadcrumb
# is better than a false breadcrumb. Paired with the same removal in
# stop-hook.sh:35.

# --- Write crash marker ---
if [ -n "$HOOK_AGENT" ]; then
    AGENT_SESSION_DIR="$(agent_dir "$HOOK_AGENT")/session"
    if [ -d "$AGENT_SESSION_DIR" ]; then
        echo "$(date +%Y-%m-%dT%H:%M:%S) context_exhaustion sid=$HOOK_SID" > "$AGENT_SESSION_DIR/crash-marker"
    fi
    # Best-effort insight capture
    printf '%s' "$STDIN_JSON" | MIND_AGENT="$HOOK_AGENT" python3 "$CORE_ROOT/scripts/capture-insights.py" 2>/dev/null || true
fi

# --- Housekeeping ---
# Cleanup stale bindings — but never delete bindings for running agents.
# Mirrors stop-hook.sh:73-81 identity-pin pattern (rb-673,  audit,  fix).
# Prior implementation `find -delete` had no running-session-id filter and could
# unbind a still-alive runner whose binding file mtime was >24h old.
for _AF in "$PROJECT_ROOT"/.active-agent-*; do
    [ -f "$_AF" ] || continue
    [ -z "$(find "$_AF" -maxdepth 0 -mmin +1440 2>/dev/null)" ] && continue
    _BA=$(cat "$_AF" 2>/dev/null | tr -d '\r\n')
    [ -n "$_BA" ] && [ -f "$(agent_dir "$_BA")/session/running-session-id" ] && continue
    rm -f "$_AF" 2>/dev/null || true
done
rm -f "$PROJECT_ROOT/.stop-hook-stdin.json"

exit 0
